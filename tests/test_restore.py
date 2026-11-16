"""Restore only disposable backups into new temporary instances, entirely offline."""
import hashlib
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import stat
from unittest.mock import Mock
import zipfile

import pytest


@pytest.fixture
def source(client, tmp_path):
    from core import database as db
    downloads = tmp_path / "source-downloads"
    folder = downloads / "Title_123" / "Chapter"
    folder.mkdir(parents=True)
    (folder / "00001.webp").write_bytes(b"published image bytes")
    db.insert_job("job_done", "123", "Keep this title", ["71"])
    db.update_job("job_done", status="completed", output_path=str(folder.parent))
    db.add_wishlist("123", "Keep this title", "Author", "")
    db.add_album_tag("123", "my-tag")
    db.set_setting("download_root", str(downloads))
    db.set_setting("proxy", "http://private-user:private-password@proxy.invalid")
    db.set_setting("timeout", "19")
    with db.transaction() as conn:
        conn.execute("INSERT INTO album_update_checks(album_id, baseline_ids, new_chapters, new_count) "
                     "VALUES ('123', '[\"71\"]', '[{\"photo_id\":\"72\"}]', 1)")
    return db.DB_PATH, downloads


def _backup(source, path, *, included=False):
    from core.backup import create_backup
    create_backup(source[0], source[1], path, include_downloads=included, service_stopped=included)
    return path


def _db(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return closing(connection)


def _rewrite_archive(path, *, manifest_change=None, database_change=None, extra=None):
    with zipfile.ZipFile(path) as archive:
        records = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(records["manifest.json"])
    if database_change:
        database = path.with_suffix(".fixture.db")
        database.write_bytes(records["app.db"])
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                database_change(connection)
                manifest["schema_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
        records["app.db"] = database.read_bytes()
        database.unlink()
        manifest["database"].update(size=len(records["app.db"]), sha256=hashlib.sha256(records["app.db"]).hexdigest())
    if manifest_change:
        manifest_change(manifest)
    records["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in records.items():
            archive.writestr(name, data)
        if extra:
            archive.writestr(*extra)


def _assert_not_published(parent, target):
    assert not target.exists()
    assert not list(parent.glob(".jmcomic-restore-*"))


def test_full_restore_maps_paths_and_preserves_non_path_data(source, tmp_path):
    from core.restore import restore_backup
    backup = _backup(source, tmp_path / "full.zip", included=True)
    original = source[0].read_bytes()
    target = tmp_path / "restored"
    result = restore_backup(backup, target)
    assert result.target == target and result.resources_included
    assert result.restored_files == 1 and result.warnings == ()
    assert (target / "downloads/Title_123/Chapter/00001.webp").read_bytes() == b"published image bytes"
    with _db(result.database) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert dict(conn.execute("SELECT title, output_path FROM jobs").fetchone()) == {
            "title": "Keep this title", "output_path": str(target / "downloads/Title_123")}
        assert conn.execute("SELECT output_path FROM resource_catalog").fetchone()[0] == str(target / "downloads/Title_123")
        assert conn.execute("SELECT value FROM settings WHERE key='download_root'").fetchone()[0] == str(target / "downloads")
        assert conn.execute("SELECT value FROM settings WHERE key='proxy'").fetchone()[0].endswith("@proxy.invalid")
        assert conn.execute("SELECT title FROM wishlist").fetchone()[0] == "Keep this title"
        assert conn.execute("SELECT tag FROM album_tags").fetchone()[0] == "my-tag"
        assert conn.execute("SELECT baseline_ids, new_count FROM album_update_checks").fetchone()[1] == 1
        # The original supported triggers survive the migration.
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0] == 3
    assert source[0].read_bytes() == original
    assert result.remapped_jobs == result.remapped_resources == 1
    assert not list(tmp_path.glob(".jmcomic-restore-*"))


def test_inventory_restore_reports_missing_resources_and_preserves_pauses(source, tmp_path):
    from core import database as db
    from core.restore import restore_backup
    for job_id, status in (("job_interrupted", "running"), ("job_paused", "paused")):
        db.insert_job(job_id, "456", "Other", ["81"])
        db.update_job(job_id, status=status, output_path=str(source[1] / "Other_456"))
    backup = _backup(source, tmp_path / "inventory.zip")
    result = restore_backup(backup, tmp_path / "inventory-restored")
    assert not result.resources_included and result.restored_files == 0
    assert result.warnings and "未包含资源文件" in result.warnings[0]
    assert list(result.downloads.iterdir()) == []
    with _db(result.database) as conn:
        statuses = dict(conn.execute("SELECT job_id,status FROM jobs"))
        assert statuses == {"job_done": "completed", "job_interrupted": "queued", "job_paused": "paused"}
    assert result.requeued_jobs == 1


@pytest.mark.parametrize("source_root,output", [
    (r"D:\Library\downloads", r"d:\library\downloads\Title_123"),
    ("/srv/library/downloads", "/srv/library/downloads/Title_123"),
    (r"\\server\share\downloads", r"\\server\share\downloads\Title_123"),
])
def test_cross_platform_source_paths_map_by_components(source, tmp_path, source_root, output):
    from core.restore import restore_backup
    backup = _backup(source, tmp_path / "portable.zip")
    def update_paths(conn):
        conn.execute("UPDATE jobs SET output_path=?", (output,))
        conn.execute("UPDATE resource_catalog SET output_path=?", (output,))
    _rewrite_archive(backup, database_change=update_paths,
                     manifest_change=lambda manifest: manifest["resources"].update(source_root=source_root))
    result = restore_backup(backup, tmp_path / "portable-restored")
    with _db(result.database) as conn:
        assert conn.execute("SELECT output_path FROM jobs").fetchone()[0] == str(result.downloads / "Title_123")


@pytest.mark.parametrize("output", [
    r"D:\Library\outside\Title_123", r"D:\Library\downloads-old\Title_123",
    r"D:\Library\downloads\..\outside", r"D:\Library\downloads\CON\Title_123",
    r"D:\Library\downloads\title:stream", "relative/path", "/different/platform/path",
])
def test_unmapped_or_unsafe_database_paths_abort_without_publication(source, tmp_path, output):
    from core.restore import restore_backup, RestoreError
    backup = _backup(source, tmp_path / "bad-path.zip")
    _rewrite_archive(backup, database_change=lambda conn: conn.execute("UPDATE jobs SET output_path=?", (output,)),
                     manifest_change=lambda manifest: manifest["resources"].update(source_root=r"D:\Library\downloads"))
    target = tmp_path / "not-created"
    with pytest.raises(RestoreError) as failure:
        restore_backup(backup, target)
    assert "private-password" not in str(failure.value)
    _assert_not_published(tmp_path, target)


def test_database_only_migration_snapshot_requires_a_source_root(source, tmp_path):
    from core.backup import create_backup
    from core.restore import restore_backup, RestoreError
    backup = tmp_path / "migration.zip"
    create_backup(source[0], None, backup)
    target = tmp_path / "not-created"
    with pytest.raises(RestoreError) as failure:
        restore_backup(backup, target)
    assert failure.value.reason == "missing_source_root"
    _assert_not_published(tmp_path, target)


def test_database_migration_snapshot_with_explicit_root_is_recoverable(source, tmp_path):
    from core.backup import create_backup
    from core.restore import restore_backup
    backup = tmp_path / "migration-root.zip"
    info = create_backup(source[0], None, backup, source_download_root=source[1])
    assert info.manifest["resources"]["mode"] == "database"
    assert info.manifest["resources"]["files"] == []
    result = restore_backup(backup, tmp_path / "migration-restored")
    assert not result.resources_included and result.restored_files == 0
    assert result.warnings and list(result.downloads.iterdir()) == []
    with _db(result.database) as conn:
        assert conn.execute("SELECT output_path FROM resource_catalog").fetchone()[0] == str(result.downloads / "Title_123")


@pytest.mark.parametrize("existing", ["empty", "database", "downloads"])
def test_existing_targets_are_never_overwritten(source, tmp_path, existing):
    from core.restore import restore_backup, RestoreError
    backup = _backup(source, tmp_path / "source.zip")
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / existing
    marker.write_bytes(b"existing instance data")
    with pytest.raises(RestoreError) as failure:
        restore_backup(backup, target)
    assert failure.value.reason == "target_exists"
    assert marker.read_bytes() == b"existing instance data"


@pytest.mark.parametrize("mutation", ["future", "missing_table", "unknown_trigger", "modified_trigger"])
def test_unsupported_schema_and_triggers_are_refused(source, tmp_path, mutation):
    from core.restore import restore_backup, RestoreError
    backup = _backup(source, tmp_path / "unsupported.zip")
    def mutate(conn):
        if mutation == "future":
            conn.execute("PRAGMA user_version=999")
        elif mutation == "missing_table":
            conn.execute("DROP TABLE resource_catalog")
        elif mutation == "unknown_trigger":
            conn.execute("CREATE TRIGGER private_trigger AFTER UPDATE ON settings BEGIN DELETE FROM wishlist; END")
        else:
            conn.execute("DROP TRIGGER catalog_completed_job_update")
            conn.execute("CREATE TRIGGER catalog_completed_job_update AFTER UPDATE ON jobs BEGIN DELETE FROM wishlist; END")
    _rewrite_archive(backup, database_change=mutate)
    target = tmp_path / "not-created"
    with pytest.raises(RestoreError):
        restore_backup(backup, target)
    _assert_not_published(tmp_path, target)


@pytest.mark.parametrize("member", ["../escape", "/absolute", r"C:\absolute", "downloads/../escape", "app.db"])
def test_invalid_zip_members_are_rejected_before_publish(source, tmp_path, member):
    from core.restore import restore_backup, RestoreError
    backup = _backup(source, tmp_path / "invalid.zip")
    with pytest.warns(UserWarning) if member == "app.db" else nullcontext():
        _rewrite_archive(backup, extra=(member, b"private-password"))
    target = tmp_path / "not-created"
    with pytest.raises(RestoreError) as failure:
        restore_backup(backup, target)
    assert failure.value.reason == "invalid_backup"
    assert "private-password" not in str(failure.value)
    _assert_not_published(tmp_path, target)


def test_archive_symlinks_and_hash_mismatch_are_rejected(source, tmp_path):
    from core.restore import restore_backup, RestoreError
    for kind in ("symlink", "hash"):
        backup = _backup(source, tmp_path / (kind + ".zip"))
        if kind == "symlink":
            info = zipfile.ZipInfo("downloads/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            _rewrite_archive(backup, extra=(info, b"outside"))
        else:
            _rewrite_archive(backup, manifest_change=lambda manifest: manifest["database"].update(sha256="0" * 64))
        target = tmp_path / (kind + "-target")
        with pytest.raises(RestoreError):
            restore_backup(backup, target)
        _assert_not_published(tmp_path, target)


def test_publish_failure_removes_only_private_stage(source, tmp_path, monkeypatch):
    from core import restore
    backup = _backup(source, tmp_path / "source.zip")
    target = tmp_path / "not-created"
    marker = tmp_path / "unrelated"
    marker.write_bytes(b"keep")
    monkeypatch.setattr(restore, "_publish_new_directory", Mock(side_effect=OSError("private-password")))
    with pytest.raises(restore.RestoreError) as failure:
        restore.restore_backup(backup, target)
    assert "private-password" not in str(failure.value)
    assert marker.read_bytes() == b"keep"
    _assert_not_published(tmp_path, target)


def test_a_target_created_during_publish_is_not_replaced(source, tmp_path, monkeypatch):
    from core import restore
    backup = _backup(source, tmp_path / "source.zip")
    target = tmp_path / "concurrent-target"
    original = restore._publish_new_directory
    def concurrent(stage, destination):
        destination.mkdir()
        original(stage, destination)
    monkeypatch.setattr(restore, "_publish_new_directory", concurrent)
    with pytest.raises(restore.RestoreError):
        restore.restore_backup(backup, target)
    assert target.is_dir() and list(target.iterdir()) == []
    assert not list(tmp_path.glob(".jmcomic-restore-*"))


def test_linked_target_parent_is_refused(source, tmp_path):
    from core.restore import restore_backup, RestoreError
    backup = _backup(source, tmp_path / "source.zip")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("file symbolic links require local permission")
    with pytest.raises(RestoreError):
        restore_backup(backup, linked / "new-instance")
    assert list(outside.iterdir()) == []


def test_case_changed_trigger_literal_is_not_treated_as_equivalent(source, tmp_path):
    from core.restore import restore_backup, RestoreError
    backup = _backup(source, tmp_path / "trigger.zip")
    def alter_trigger(conn):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='catalog_completed_job_update'").fetchone()[0]
        conn.execute("DROP TRIGGER catalog_completed_job_update")
        conn.execute(sql.replace("'completed'", "'COMPLETED'"))
    _rewrite_archive(backup, database_change=alter_trigger)
    target = tmp_path / "not-created"
    with pytest.raises(RestoreError) as failure:
        restore_backup(backup, target)
    assert failure.value.reason == "unsupported_schema"
    _assert_not_published(tmp_path, target)


def test_changed_private_subdirectory_is_not_deleted_during_failed_cleanup(source, tmp_path, monkeypatch):
    from core import restore
    backup = _backup(source, tmp_path / "source.zip", included=True)
    target = tmp_path / "not-created"
    original = restore._rewrite_database
    foreign = []
    def replace_subdirectory(database, *args):
        result = original(database, *args)
        moved = database.parent.with_name("original-data")
        database.parent.rename(moved)
        database.parent.mkdir()
        marker = database.parent / "foreign-owner-marker"
        marker.write_bytes(b"must not delete")
        foreign.append(marker)
        return result
    monkeypatch.setattr(restore, "_rewrite_database", replace_subdirectory)
    with pytest.raises(restore.RestoreError):
        restore.restore_backup(backup, target)
    assert not target.exists()
    assert foreign[0].read_bytes() == b"must not delete"


def test_zero_budget_publishes_nothing(source, tmp_path):
    from core.restore import restore_backup, RestoreTimeout
    backup = _backup(source, tmp_path / "source.zip")
    target = tmp_path / "not-created"
    with pytest.raises(RestoreTimeout):
        restore_backup(backup, target, timeout=0)
    _assert_not_published(tmp_path, target)
