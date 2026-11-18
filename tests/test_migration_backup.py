"""Preserve an old database before the first initialization write, not after DDL."""
from contextlib import closing
import sqlite3
import zipfile

import pytest

from core.database_schema import PUBLICATION_DDL

DROP_PUBLICATION_SQL = "".join(f'DROP {ddl.split()[1]} "{name}";' for name, ddl in reversed(PUBLICATION_DDL.items()))


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
    target.executescript(DROP_PUBLICATION_SQL)
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION
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


def test_automatic_snapshot_preserves_unmappable_paths_for_diagnosis(legacy, tmp_path, monkeypatch):
    from core import backup, database as db
    from core.restore import RestoreError, restore_backup
    database, backups = legacy
    old_path = str(tmp_path / "old-downloads" / "Old title")
    new_root = tmp_path / "new-downloads"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE jobs SET output_path=? WHERE job_id='old_job'", (old_path,))
    monkeypatch.setattr(db, "get_download_root", lambda: new_root)
    monkeypatch.setattr(backup, "_inventory", lambda *args: pytest.fail("migration scanned resources"))
    db.init_db()
    archive, = backups.glob(f"pre-migration-v2-to-v{db.CURRENT_SCHEMA_VERSION}-*.zip")
    info = backup.inspect_backup(archive)
    assert info.schema_version == 2 and info.manifest["resources"]["mode"] == "database"
    assert info.manifest["resources"]["source_root"] == str(new_root) and not new_root.exists()
    saved = tmp_path / "diagnostic.db"
    with zipfile.ZipFile(archive) as package:
        saved.write_bytes(package.read("app.db"))
    with closing(sqlite3.connect(saved)) as connection:
        assert connection.execute("SELECT output_path FROM jobs WHERE job_id='old_job'").fetchone() == (old_path,)
    target = tmp_path / "not-restorable"
    with pytest.raises(RestoreError) as failure:
        restore_backup(archive, target)
    assert failure.value.reason == "unmapped_path" and not target.exists()


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
