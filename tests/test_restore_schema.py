"""F24/F30: reject valid SQLite with untrusted schemas before publishing a restore.

All archives, source databases and resource bytes belong to disposable fixtures.
The payload preflight deliberately distinguishes schema attacks from corrupt
SQLite or stale ZIP hashes; the public restore API is the acceptance boundary.
"""
from contextlib import closing
import hashlib
import json
import sqlite3
import zipfile

import pytest

from test_restore import source, _backup, _rewrite_archive, _assert_not_published


def _source_state(source):
    database, downloads = source
    paths = [database, *(database.with_name(database.name + suffix)
                         for suffix in ("-wal", "-shm", "-journal"))]
    return (
        {path.name: hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in paths},
        {path.relative_to(downloads).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
         for path in downloads.rglob("*") if path.is_file()},
    )


def _assert_valid_payload(backup, tmp_path, version=4):
    """Reopen the rewritten DDL with trusted_schema OFF, as production does."""
    with zipfile.ZipFile(backup) as archive:
        assert archive.testzip() is None
        payload = archive.read("app.db")
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["database"]["size"] == len(payload)
    assert manifest["database"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["schema_version"] == version
    database = tmp_path / "payload-preflight.db"
    database.write_bytes(payload)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA query_only=ON")
            assert conn.execute("PRAGMA user_version").fetchone()[0] == version
            assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("SELECT job_id, status FROM jobs").fetchall()
    finally:
        database.unlink()


def _replace_table_fragment(conn, table, old, new):
    """Change only DDL; the next connection must parse the resulting schema."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone()[0]
    assert sql.count(old) == 1, (table, old)
    changed = sql.replace(old, new)
    assert changed != sql
    cookie = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema=ON")
    try:
        assert conn.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
                            (changed, table)).rowcount == 1
        conn.execute(f"PRAGMA schema_version={cookie + 1}")
    finally:
        conn.execute("PRAGMA writable_schema=OFF")


def _assert_schema_refused(source, backup, tmp_path):
    from core.restore import RestoreError, restore_backup
    before = _source_state(source)
    archive_before = backup.read_bytes()
    target = tmp_path / "schema-target-only"
    try:
        with pytest.raises(RestoreError) as failure:
            restore_backup(backup, target)
        assert failure.value.reason == "unsupported_schema"
    finally:
        assert _source_state(source) == before
        assert backup.read_bytes() == archive_before
        _assert_not_published(tmp_path, target)


@pytest.mark.parametrize("table,column", [
    ("jobs", "status"),
    ("resource_catalog", "album_id"),
    ("wishlist", "title"),
])
def test_extra_unique_index_is_an_unsupported_schema(source, tmp_path, table, column):
    backup = _backup(source, tmp_path / "extra-unique.zip")
    _rewrite_archive(backup, database_change=lambda conn: conn.execute(
        f"CREATE UNIQUE INDEX private_unique ON {table}({column})"))
    _assert_valid_payload(backup, tmp_path)
    _assert_schema_refused(source, backup, tmp_path)


@pytest.mark.parametrize("index,definition,unique", [
    pytest.param("idx_jobs_status", "ON jobs(album_id)", False, id="wrong-column"),
    pytest.param("idx_jobs_status", "ON wishlist(download_status)", False, id="wrong-table"),
    pytest.param("idx_jobs_status", "ON jobs(status)", True, id="changed-uniqueness"),
    pytest.param("idx_jobs_created", "ON jobs(created_at DESC)", False, id="changed-order"),
    pytest.param("idx_jobs_status", "ON jobs(status COLLATE NOCASE)", False, id="changed-collation"),
    pytest.param("idx_jobs_status", "ON jobs(lower(status))", False, id="expression"),
    pytest.param("idx_jobs_status", "ON jobs(status) WHERE status='completed'", False, id="partial"),
])
def test_standard_index_name_does_not_authorize_changed_definition(source, tmp_path, index, definition,
                                                                 unique):
    backup = _backup(source, tmp_path / "wrong-index.zip")

    def mutate(conn):
        conn.execute(f"DROP INDEX {index}")
        qualifier = "UNIQUE " if unique else ""
        conn.execute(f"CREATE {qualifier}INDEX {index} {definition}")

    _rewrite_archive(backup, database_change=mutate)
    _assert_valid_payload(backup, tmp_path)
    _assert_schema_refused(source, backup, tmp_path)


@pytest.mark.parametrize("table,old,new", [
    pytest.param("jobs", "DEFAULT 'queued'", "DEFAULT 'paused'", id="jobs-default"),
    pytest.param("settings", "value TEXT", "value TEXT DEFAULT 'untrusted'", id="settings-default"),
    pytest.param("wishlist", "DEFAULT 'none'", "DEFAULT 'done'", id="wishlist-default"),
    pytest.param("album_meta", "title          TEXT DEFAULT ''",
                 "title          TEXT DEFAULT 'untrusted'", id="metadata-default"),
    pytest.param("jobs", "output_path TEXT", "output_path TEXT CHECK (length(output_path) < 100000)",
                 id="check-accepting-existing-rows"),
    pytest.param("jobs", "output_path TEXT",
                 "output_path TEXT CHECK (output_path IS NULL OR instr(output_path, 'schema-target-only') = 0)",
                 id="check-rejecting-only-remapped-path"),
    pytest.param("jobs", "status TEXT", "status TEXT COLLATE NOCASE", id="jobs-collation"),
    pytest.param("settings", "value TEXT", "value TEXT COLLATE RTRIM", id="settings-collation"),
    pytest.param("jobs", "album_id TEXT NOT NULL", 'album_id TEXT "NOT" NULL',
                 id="quoted-keyword-is-not-notnull-constraint"),
    pytest.param("settings", "key TEXT PRIMARY KEY", "Key TEXT PRIMARY KEY",
                 id="unicode-identifier-is-not-ascii-key"),
    pytest.param("wishlist", "title TEXT", "title TEXT COLLATE NOCASE", id="wishlist-collation"),
    pytest.param("jobs", "album_id TEXT NOT NULL", "album_id TEXT NOT NULL REFERENCES wishlist(album_id)",
                 id="jobs-foreign-key"),
    pytest.param("resource_catalog", "job_id TEXT PRIMARY KEY",
                 "job_id TEXT PRIMARY KEY REFERENCES jobs(job_id)", id="catalog-foreign-key"),
])
def test_valid_sqlite_with_altered_table_semantics_is_refused(source, tmp_path, table, old, new):
    backup = _backup(source, tmp_path / "changed-ddl.zip")
    _rewrite_archive(backup, database_change=lambda conn: _replace_table_fragment(conn, table, old, new))
    _assert_valid_payload(backup, tmp_path)
    _assert_schema_refused(source, backup, tmp_path)


def test_quoted_primary_keyword_does_not_hide_a_missing_constraint(source, tmp_path):
    backup = _backup(source, tmp_path / "quoted-primary.zip")

    def mutate(conn):
        rows = conn.execute("SELECT key,value FROM settings").fetchall()
        conn.execute("DROP TABLE settings")
        conn.execute('CREATE TABLE settings(key TEXT "PRIMARY" KEY,value TEXT)')
        conn.executemany("INSERT INTO settings VALUES (?,?)", rows)

    _rewrite_archive(backup, database_change=mutate)
    _assert_valid_payload(backup, tmp_path)
    _assert_schema_refused(source, backup, tmp_path)


def test_null_resource_primary_key_still_gets_its_path_remapped(source, tmp_path):
    from core.restore import restore_backup
    backup = _backup(source, tmp_path / "null-resource-key.zip")
    original_path = str(source[1] / "Title_123")
    _rewrite_archive(backup, database_change=lambda conn: conn.execute(
        "INSERT INTO resource_catalog(job_id,job_order,album_id,output_path) VALUES(NULL,99,'123',?)",
        (original_path,)))
    _assert_valid_payload(backup, tmp_path)
    result = restore_backup(backup, tmp_path / "null-key-restored")
    assert result.remapped_resources == 2
    with closing(sqlite3.connect(result.database)) as connection:
        assert connection.execute("SELECT output_path FROM resource_catalog WHERE job_id IS NULL").fetchone() == (
            str(result.downloads / "Title_123"),)


def test_v3_restored_catalog_survives_first_startup_queue_backfill(source, tmp_path, monkeypatch):
    from core import database as db
    from core.restore import restore_backup
    backup = _backup(source, tmp_path / "v3-independent-catalog.zip")

    def mutate(conn):
        conn.execute("UPDATE jobs SET queued_at=NULL")
        conn.execute("UPDATE resource_catalog SET title='Independent title',superseded_at='2026-01-02',"
                     "output_path=? WHERE job_id='job_done'", (str(source[1] / "Independent_123"),))
        conn.execute("PRAGMA user_version=3")

    _rewrite_archive(backup, database_change=mutate)
    _assert_valid_payload(backup, tmp_path, version=3)
    result = restore_backup(backup, tmp_path / "v3-first-startup")
    monkeypatch.setattr(db, "DB_DIR", result.database.parent)
    monkeypatch.setattr(db, "DB_PATH", result.database)
    monkeypatch.setattr(db, "get_backup_dir", lambda: tmp_path / "first-startup-snapshot")
    monkeypatch.setattr(db, "get_download_root", lambda: result.downloads)
    db.init_db()
    with closing(sqlite3.connect(result.database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT title,superseded_at,output_path FROM resource_catalog "
                                  "WHERE job_id='job_done'").fetchone() == (
            "Independent title", "2026-01-02", str(result.downloads / "Independent_123"))


@pytest.mark.parametrize("table,expression", [
    ("jobs", "lower(status)"),
    ("settings", "length(value)"),
    ("wishlist", "lower(title)"),
])
def test_generated_columns_hidden_from_table_info_are_refused(source, tmp_path, table, expression):
    backup = _backup(source, tmp_path / "generated.zip")

    def mutate(conn):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN private_generated TEXT "
                     f"GENERATED ALWAYS AS ({expression}) VIRTUAL")
        assert "private_generated" not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert "private_generated" in {row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})")}

    _rewrite_archive(backup, database_change=mutate)
    _assert_valid_payload(backup, tmp_path)
    _assert_schema_refused(source, backup, tmp_path)


@pytest.mark.parametrize("kind", ["view", "virtual-table"])
def test_views_and_virtual_tables_are_refused(source, tmp_path, kind):
    backup = _backup(source, tmp_path / "extra-object.zip")

    def mutate(conn):
        if kind == "view":
            conn.execute("CREATE VIEW private_projection AS SELECT job_id, status FROM jobs")
        else:
            # Use a known, empty table name to exercise the virtual-table rule.
            conn.execute("DROP TABLE album_detail_cache")
            conn.execute("CREATE VIRTUAL TABLE album_detail_cache USING fts5("
                         "album_id, detail_json, cover_cdn_url, cached_at)")

    _rewrite_archive(backup, database_change=mutate)
    _assert_valid_payload(backup, tmp_path)
    _assert_schema_refused(source, backup, tmp_path)


@pytest.mark.parametrize("table", [
    "jobs", "settings", "resource_catalog", "search_history", "wishlist", "album_tags",
    "album_detail_cache", "album_meta", "album_update_checks", "update_check_log",
])
def test_unknown_columns_in_every_application_table_are_refused(source, tmp_path, table):
    backup = _backup(source, tmp_path / "unknown-column.zip")
    _rewrite_archive(backup, database_change=lambda conn: conn.execute(
        f"ALTER TABLE {table} ADD COLUMN private_extra TEXT"))
    _assert_valid_payload(backup, tmp_path)
    _assert_schema_refused(source, backup, tmp_path)


@pytest.mark.parametrize("trigger", [
    "catalog_completed_job_insert", "catalog_completed_job_update", "catalog_revoked_completion",
])
@pytest.mark.parametrize("change", ["literal-case", "body"])
def test_each_standard_trigger_must_keep_its_trusted_definition(source, tmp_path, trigger, change):
    backup = _backup(source, tmp_path / "changed-trigger.zip")

    def mutate(conn):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                           (trigger,)).fetchone()[0]
        if change == "literal-case":
            changed = sql.replace("'completed'", "'COMPLETED'")
        elif trigger == "catalog_revoked_completion":
            changed = sql.replace("WHERE job_id = NEW.job_id", "WHERE job_id != NEW.job_id")
        else:
            changed = sql.replace("NEW.title", "'untrusted title'")
        assert changed != sql
        conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(changed)

    _rewrite_archive(backup, database_change=mutate)
    _assert_valid_payload(backup, tmp_path)
    _assert_schema_refused(source, backup, tmp_path)


def _seed_queue(source):
    from core import database as db
    rows = [
        ("old_running", "running", "2020-01-01T00:00:00"),
        ("next_running", "running", "2020-01-02T00:00:00"),
        ("waiting", "queued", "2020-01-03T00:00:00"),
        ("paused", "paused", "2020-01-04T00:00:00"),
    ]
    for job_id, status, created in rows:
        db.insert_job(job_id, "456", "Keep queue title", ["81"])
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET status=?, created_at=?, queued_at=?, output_path=? WHERE job_id=?",
                         (status, created, "2099-01-01T00:00:00" if job_id == "waiting" else created,
                          str(source[1] / "Other_456"), job_id))


def _assert_preserved_data(result, *, version, queue):
    with closing(sqlite3.connect(result.database)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT title, selected_photo_ids, output_path FROM jobs WHERE job_id='job_done'").fetchone() == (
            "Keep this title", '["71"]', str(result.downloads / "Title_123"))
        assert conn.execute("SELECT value FROM settings WHERE key='download_root'").fetchone()[0] == str(result.downloads)
        assert conn.execute("SELECT value FROM settings WHERE key='timeout'").fetchone()[0] == "19"
        assert conn.execute("SELECT title FROM wishlist WHERE album_id='123'").fetchone()[0] == "Keep this title"
        assert conn.execute("SELECT tag FROM album_tags WHERE album_id='123'").fetchone()[0] == "my-tag"
        assert conn.execute("SELECT baseline_ids, new_count FROM album_update_checks WHERE album_id='123'").fetchone() == (
            '["71"]', 1)
        if version == 4:
            assert conn.execute("SELECT output_path FROM resource_catalog WHERE job_id='job_done'").fetchone()[0] == str(
                result.downloads / "Title_123")
            assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0] == 3
        else:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='resource_catalog'").fetchone() is None
        if queue:
            assert dict(conn.execute("SELECT job_id, status FROM jobs")) == {
                "job_done": "completed", "old_running": "queued", "next_running": "queued",
                "waiting": "queued", "paused": "paused",
            }
            assert conn.execute("SELECT DISTINCT output_path FROM jobs WHERE job_id!='job_done'").fetchall() == [
                (str(result.downloads / "Other_456"),)]
            assert conn.execute("SELECT created_at FROM jobs WHERE job_id='old_running'").fetchone()[0] == "2020-01-01T00:00:00"
            if version == 4:
                assert [row[0] for row in conn.execute(
                    "SELECT job_id FROM jobs WHERE status='queued' "
                    "ORDER BY COALESCE(NULLIF(queued_at,''),created_at,''), id").fetchall()] == [
                        "waiting", "old_running", "next_running"]
    assert result.requeued_jobs == (2 if queue else 0)


@pytest.mark.parametrize("included", [False, True], ids=["inventory-with-queue", "full-with-resources"])
def test_current_v4_schema_restores_with_existing_data_contracts(source, tmp_path, included):
    from core.restore import restore_backup
    if not included:
        _seed_queue(source)
    backup = _backup(source, tmp_path / "valid-v4.zip", included=included)
    _assert_valid_payload(backup, tmp_path)
    before, archive_before = _source_state(source), backup.read_bytes()
    result = restore_backup(backup, tmp_path / "valid-v4-instance")
    _assert_preserved_data(result, version=4, queue=not included)
    assert result.resources_included is included
    assert result.restored_files == (1 if included else 0)
    assert bool(result.warnings) is not included
    if included:
        assert (result.downloads / "Title_123/Chapter/00001.webp").read_bytes() == b"published image bytes"
    else:
        assert list(result.downloads.iterdir()) == []
    assert _source_state(source) == before
    assert backup.read_bytes() == archive_before
    assert not list(tmp_path.glob(".jmcomic-restore-*"))


@pytest.mark.parametrize("keep_queue_column", [False, True], ids=["historical-v2", "existing-v2-fixture-shape"])
def test_automatic_v2_snapshot_restores_queue_pause_and_paths(source, tmp_path, monkeypatch, keep_queue_column):
    from core import database as db
    from core.restore import restore_backup
    _seed_queue(source)
    legacy_dir = tmp_path / "legacy-instance"
    legacy_dir.mkdir()
    legacy = legacy_dir / "app.db"
    with closing(sqlite3.connect(source[0].as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(legacy)) as conn:
            original.backup(conn)
            for trigger in ("catalog_completed_job_insert", "catalog_completed_job_update", "catalog_revoked_completion"):
                conn.execute(f"DROP TRIGGER {trigger}")
            conn.execute("DROP TABLE resource_catalog")
            if not keep_queue_column:
                conn.execute("DROP INDEX idx_jobs_queue")
                conn.execute("ALTER TABLE jobs DROP COLUMN queued_at")
            conn.execute("PRAGMA user_version=2")
            conn.commit()
    backups = tmp_path / "automatic-backups"
    monkeypatch.setattr(db, "DB_DIR", legacy_dir)
    monkeypatch.setattr(db, "DB_PATH", legacy)
    monkeypatch.setattr(db, "get_backup_dir", lambda: backups)
    monkeypatch.setattr(db, "get_download_root", lambda: source[1])
    db.init_db()
    archives = list(backups.glob("pre-migration-v2-to-v4-*.zip"))
    assert len(archives) == 1
    backup = archives[0]
    _assert_valid_payload(backup, tmp_path, version=2)
    before = _source_state(source)
    archive_before, legacy_before = backup.read_bytes(), legacy.read_bytes()
    result = restore_backup(backup, tmp_path / "valid-v2-instance")
    _assert_preserved_data(result, version=2, queue=True)
    assert result.resources_included is False and result.restored_files == 0 and result.warnings
    assert list(result.downloads.iterdir()) == []
    assert _source_state(source) == before
    assert legacy.read_bytes() == legacy_before
    assert backup.read_bytes() == archive_before
    assert list(backups.glob("*.zip")) == [backup]
    assert not list(tmp_path.glob(".jmcomic-restore-*"))
