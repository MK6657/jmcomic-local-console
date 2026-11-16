"""Preserve an old database before the first initialization write, not after DDL."""
import sqlite3
import zipfile

import pytest


@pytest.fixture
def legacy(client, tmp_path, monkeypatch):
    from core import database as db
    directory = tmp_path / "old-instance"
    directory.mkdir()
    database = directory / "app.db"
    source = db.get_db()
    target = sqlite3.connect(database)
    source.backup(target)
    source.close()
    for trigger in ("catalog_completed_job_insert", "catalog_completed_job_update", "catalog_revoked_completion"):
        target.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    target.execute("DROP TABLE resource_catalog")
    target.execute("PRAGMA user_version = 2")
    target.execute("INSERT INTO jobs (job_id, album_id, title, status) VALUES ('old_job', '123', 'Keep title', 'completed')")
    target.commit()
    target.close()
    monkeypatch.setattr(db, "DB_DIR", directory)
    monkeypatch.setattr(db, "DB_PATH", database)
    backups = tmp_path / "backups"
    monkeypatch.setattr(db, "get_backup_dir", lambda: backups)
    return database, backups


def test_original_schema_is_backed_up_before_migration_and_not_again_on_next_init(legacy, tmp_path):
    from core import database as db
    database, backups = legacy
    db.init_db()
    archives = list(backups.glob("pre-migration-*.zip"))
    assert len(archives) == 1
    before = tmp_path / "before.db"
    with zipfile.ZipFile(archives[0]) as archive:
        before.write_bytes(archive.read("app.db"))
    with sqlite3.connect(before) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT title FROM jobs WHERE job_id='old_job'").fetchone()[0] == "Keep title"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='resource_catalog'").fetchone() is None
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT title FROM resource_catalog").fetchone()[0] == "Keep title"
    db.init_db()
    assert list(backups.glob("*.zip")) == archives
    # Exercise the actual automatic v2 snapshot through the public restore path,
    # not only a separately manufactured backup. It remains a DB-only restore.
    from core.restore import restore_backup
    restored = restore_backup(archives[0], tmp_path / "recovered-before-migration")
    assert restored.resources_included is False and restored.warnings
    with sqlite3.connect(restored.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT title FROM jobs WHERE job_id='old_job'").fetchone()[0] == "Keep title"


def test_backup_failure_aborts_all_initialization_writes(legacy, monkeypatch):
    from core import backup, database as db
    database, _ = legacy
    before = database.read_bytes()
    def refuse(*args, **kwargs):
        raise OSError("backup destination unavailable")
    monkeypatch.setattr(backup, "create_backup", refuse)
    with pytest.raises(OSError, match="backup destination"):
        db.init_db()
    assert database.read_bytes() == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='resource_catalog'").fetchone() is None


def test_future_schema_is_rejected_without_modifying_database(legacy):
    from core import database as db
    database, backups = legacy
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA user_version = 999")
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="版本高于"):
        db.init_db()
    assert database.read_bytes() == before
    assert not backups.exists()
