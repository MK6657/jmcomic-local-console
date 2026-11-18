"""Reject damaged current schemas before WAL configuration, DDL or backfill."""
from contextlib import closing
import sqlite3
from unittest.mock import Mock

import pytest

from core import database as db
from core.database_schema import validate_schema
from test_migration_backup import DROP_PUBLICATION_SQL


@pytest.fixture
def instance(client, tmp_path, monkeypatch):
    # Copy from the isolated fixture, then mutate only a separate ordinary DB.
    directory = tmp_path / "schema-instance"
    directory.mkdir()
    path = directory / "app.db"
    with closing(db.get_db()) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
    monkeypatch.setattr(db, "DB_DIR", directory)
    monkeypatch.setattr(db, "DB_PATH", path)
    backups = tmp_path / "pre-migration"
    monkeypatch.setattr(db, "get_backup_dir", lambda: backups)
    return path, backups


def mutate(path, statement):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.executescript(statement)


@pytest.mark.parametrize("statement", [
    "DROP TABLE jobs", "DROP TABLE settings", "DROP TABLE resource_catalog", "DROP TABLE published_tasks",
    "DROP TABLE album_maintenance", "DROP INDEX idx_published_album", "DROP TABLE task_page_receipts",
    "DROP TABLE album_update_checks", "DROP TABLE update_check_log",
    "DROP TRIGGER catalog_completed_job_insert", "DROP TRIGGER catalog_completed_job_update",
    "DROP TRIGGER catalog_revoked_completion", "DROP INDEX idx_jobs_queue",
    "DROP INDEX idx_resource_catalog_album_latest", "DROP INDEX idx_jobs_status",
    "DROP INDEX idx_jobs_status; CREATE INDEX idx_jobs_status ON jobs(album_id)",
    "CREATE UNIQUE INDEX unexpected_unique_album ON jobs(album_id)",
    "CREATE VIEW unexpected_view AS SELECT * FROM settings",
    "ALTER TABLE settings ADD COLUMN unexpected TEXT",
    "DROP TRIGGER catalog_completed_job_update; CREATE TRIGGER catalog_completed_job_update "
    "AFTER UPDATE ON jobs BEGIN DELETE FROM wishlist; END",
])
def test_current_schema_mismatch_fails_before_any_initialization_write(instance, monkeypatch, statement):
    from core import backup
    path, backups = instance
    mutate(path, statement)
    before = {file.name: file.read_bytes() for file in path.parent.iterdir()}
    connect = Mock(side_effect=AssertionError("must validate before WAL or writable connection"))
    snapshot = Mock(side_effect=AssertionError("do not fabricate a migration for a corrupt current schema"))
    monkeypatch.setattr(db, "get_db", connect)
    monkeypatch.setattr(backup, "create_backup", snapshot)
    with pytest.raises(RuntimeError, match="数据库结构校验失败"):
        db.init_db()
    connect.assert_not_called()
    snapshot.assert_not_called()
    assert path.read_bytes() == before[path.name]
    # A read-only WAL connection can create SQLite's empty WAL/coordination
    # files. It must not append transaction frames or change application data.
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        assert wal.read_bytes() == before.get(wal.name, b"")
    assert not backups.exists()


def test_normal_current_startup_stays_idempotent_and_preserves_independent_catalog(instance):
    path, backups = instance
    with closing(db.get_db()) as connection:
        with connection:
            connection.execute("INSERT INTO resource_catalog(job_id,job_order,album_id,title) "
                               "VALUES('history-deleted',42,'123','Keep catalog')")
    db.init_db()
    db.init_db()
    with closing(db.get_db()) as connection:
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert connection.execute("SELECT title FROM resource_catalog").fetchone()[0] == "Keep catalog"
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        validate_schema(connection, db.CURRENT_SCHEMA_VERSION)
    assert not backups.exists()


def test_normal_legacy_v3_can_add_missing_unversioned_tables_after_snapshot(instance):
    path, backups = instance
    mutate(path, DROP_PUBLICATION_SQL + "DROP TABLE album_update_checks; DROP TABLE update_check_log; PRAGMA user_version=3;")
    before = path.read_bytes()
    db.init_db()
    archives = list(backups.glob(f"pre-migration-v3-to-v{db.CURRENT_SCHEMA_VERSION}-*.zip"))
    assert len(archives) == 1
    import zipfile
    with zipfile.ZipFile(archives[0]) as archive:
        snapshot = path.parent / "snapshot.db"
        snapshot.write_bytes(archive.read("app.db"))
    with closing(sqlite3.connect(snapshot)) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 3
        assert old.execute("SELECT name FROM sqlite_schema WHERE name='album_update_checks'").fetchone() is None
    assert before != path.read_bytes()
    with closing(db.get_db()) as current:
        validate_schema(current, db.CURRENT_SCHEMA_VERSION)


def test_analyze_statistics_and_identifier_quoting_are_compatible(instance):
    path, _ = instance
    mutate(path, 'DROP INDEX idx_jobs_status; CREATE INDEX "idx_jobs_status" ON "jobs"("status"); ANALYZE;')
    assert db._existing_schema() == db.CURRENT_SCHEMA_VERSION


def test_genuine_v4_migration_preserves_catalog_and_does_not_invent_task_snapshots(instance, tmp_path):
    from core.restore import restore_backup
    path, backups = instance
    mutate(path, DROP_PUBLICATION_SQL + "PRAGMA user_version=4; "
           "INSERT INTO jobs(job_id,album_id,status) VALUES('legacy','123','completed'); "
           "UPDATE resource_catalog SET title='Independent catalog'; DELETE FROM jobs;")
    assert db._existing_schema() == 4
    db.init_db()
    db.init_db()
    archive, = backups.glob(f"pre-migration-v4-to-v{db.CURRENT_SCHEMA_VERSION}-*.zip")
    restored = restore_backup(archive, tmp_path / "before-v5")
    with closing(sqlite3.connect(restored.database)) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 4
        validate_schema(old, 4)
    with closing(db.get_db()) as current:
        validate_schema(current, db.CURRENT_SCHEMA_VERSION)
        assert current.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION
        assert current.execute("SELECT title FROM resource_catalog").fetchone()[0] == "Independent catalog"
        assert current.execute("SELECT COUNT(*) FROM published_tasks").fetchone()[0] == 0
        assert current.execute("SELECT COUNT(*) FROM album_maintenance").fetchone()[0] == 0
        assert current.execute("SELECT COUNT(*) FROM task_page_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["DROP INDEX idx_jobs_queue", "CREATE TABLE published_tasks(private TEXT)"])
def test_v4_preflight_remains_strict_before_v5_migration(instance, damage):
    path, backups = instance
    mutate(path, DROP_PUBLICATION_SQL + "PRAGMA user_version=4; " + damage)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="结构校验失败"):
        db.init_db()
    assert path.read_bytes() == before and not backups.exists()


@pytest.mark.parametrize("bad_sql", [
    "CREATE TABLE settings(key TEXT PRIMARY KEY COLLATE NOCASE,value TEXT)",
    "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT CHECK(value!='bad'))",
    "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT DEFAULT 'new default')",
])
def test_table_constraints_are_part_of_contract(instance, bad_sql):
    path, _ = instance
    mutate(path, "DROP TABLE settings; " + bad_sql)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="结构校验失败"):
        db.init_db()
    assert path.read_bytes() == before


@pytest.mark.parametrize("definition", [
    'CREATE TABLE settings(key TEXT "PRIMARY" KEY,value TEXT)',
    'CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT "NOT" NULL)',
    'CREATE TABLE settings(key TEXT PRIMARY KEY,value TEΧT)',  # Greek chi, distinct affinity
])
def test_quoted_keywords_and_unicode_are_not_normalized_to_trusted_sql(instance, definition):
    path, _ = instance
    mutate(path, "DROP TABLE settings; " + definition)
    with pytest.raises(RuntimeError, match="结构校验失败"):
        db.init_db()


def test_unicode_identifier_casefold_does_not_hide_a_missing_column(instance):
    path, _ = instance
    # SQLite does not equate the Kelvin sign with ASCII k, unlike casefold().
    mutate(path, "DROP TABLE settings; CREATE TABLE settings(Key TEXT PRIMARY KEY, value TEXT)")
    with pytest.raises(RuntimeError, match="结构校验失败"):
        db.init_db()


@pytest.mark.parametrize("version", [0, 1, 2, 3])
def test_supported_historical_versions_migrate_from_real_column_layout(instance, version):
    path, backups = instance
    statements = [DROP_PUBLICATION_SQL, "DROP INDEX idx_jobs_queue", "ALTER TABLE jobs DROP COLUMN queued_at"]
    if version < 3:
        statements += [f"DROP TRIGGER {name}" for name in (
            "catalog_completed_job_insert", "catalog_completed_job_update", "catalog_revoked_completion")]
        statements += ["DROP TABLE resource_catalog", "ALTER TABLE jobs DROP COLUMN superseded_at"]
    if version < 2:
        statements += ["DROP TABLE album_detail_cache"]
    statements += [f"PRAGMA user_version={version}"]
    mutate(path, ";".join(statements))
    db.init_db()
    assert len(list(backups.glob(f"pre-migration-v{version}-to-v{db.CURRENT_SCHEMA_VERSION}-*.zip"))) == 1
    with closing(db.get_db()) as connection:
        validate_schema(connection, db.CURRENT_SCHEMA_VERSION)
    db.init_db()
    assert len(list(backups.glob("*.zip"))) == 1


def test_current_schema_checks_committed_wal_before_opening_writer(instance, monkeypatch):
    path, _ = instance
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("DROP TRIGGER catalog_completed_job_update")
        writer.commit()
        assert path.with_name(path.name + "-wal").stat().st_size > 32
        main_before = path.read_bytes()
        wal_before = path.with_name(path.name + "-wal").read_bytes()
        monkeypatch.setattr(db, "get_db", Mock(side_effect=AssertionError("no writer")))
        with pytest.raises(RuntimeError, match="结构校验失败"):
            db.init_db()
        assert path.read_bytes() == main_before
        assert path.with_name(path.name + "-wal").read_bytes() == wal_before


def test_internal_table_constraints_are_not_implicitly_trusted(instance):
    path, _ = instance
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute("UPDATE sqlite_schema SET sql=? WHERE name='sqlite_sequence'",
                               ("CREATE TABLE sqlite_sequence(name,seq CHECK(seq<1000000))",))
            cookie = connection.execute("PRAGMA schema_version").fetchone()[0]
            connection.execute(f"PRAGMA schema_version={cookie + 1}")
    with pytest.raises(RuntimeError, match="结构校验失败"):
        db.init_db()


def test_missing_internal_autoincrement_table_is_refused(instance):
    path, _ = instance
    mutate(path, "PRAGMA writable_schema=ON; DELETE FROM sqlite_schema WHERE name='sqlite_sequence'; "
                 "PRAGMA schema_version=1000; PRAGMA writable_schema=OFF;")
    with pytest.raises(RuntimeError, match="结构校验失败"):
        db.init_db()


@pytest.mark.parametrize("version", [0, 1, 2])
def test_historical_schema_still_requires_original_settings_table(instance, monkeypatch, version):
    path, backups = instance
    mutate(path, DROP_PUBLICATION_SQL + "DROP TRIGGER catalog_completed_job_insert; DROP TRIGGER catalog_completed_job_update; "
                 "DROP TRIGGER catalog_revoked_completion; DROP TABLE resource_catalog; "
                 f"DROP TABLE settings; PRAGMA user_version={version}")
    before = path.read_bytes()
    monkeypatch.setattr(db, "get_db", Mock(side_effect=AssertionError("no writer")))
    with pytest.raises(RuntimeError, match="结构校验失败"):
        db.init_db()
    assert path.read_bytes() == before and not backups.exists()


def test_v3_missing_catalog_index_is_refused_before_migration(instance, monkeypatch):
    path, backups = instance
    mutate(path, DROP_PUBLICATION_SQL + "DROP INDEX idx_resource_catalog_album_latest; PRAGMA user_version=3")
    before = path.read_bytes()
    monkeypatch.setattr(db, "get_db", Mock(side_effect=AssertionError("no writer")))
    with pytest.raises(RuntimeError, match="结构校验失败"):
        db.init_db()
    assert path.read_bytes() == before and not backups.exists()


def test_queue_migration_failure_rolls_back_schema_backfill_and_triggers(instance):
    from core.database_schema import QUEUE_INDEX_SQL
    path, _ = instance
    mutate(path, DROP_PUBLICATION_SQL + "DROP INDEX idx_jobs_queue; ALTER TABLE jobs DROP COLUMN queued_at; PRAGMA user_version=3")

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == QUEUE_INDEX_SQL:
                raise sqlite3.OperationalError("queue index failure")
            return super().execute(sql, *args, **kwargs)

    with closing(sqlite3.connect(path, factory=FailingConnection)) as connection:
        with pytest.raises(sqlite3.OperationalError, match="queue index failure"):
            db._run_migrations(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "queued_at" not in {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        validate_schema(connection, 3)
