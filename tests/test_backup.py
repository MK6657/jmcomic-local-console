"""Backups use only disposable SQLite databases and resource directories."""
import hashlib
import json
import os
import sqlite3
import stat
import time
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest


def database(path, download_root):
    from core.database_schema import BASE_SCHEMA_SQL
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(BASE_SCHEMA_SQL)
        connection.execute("INSERT INTO jobs(job_id, album_id, status, output_path) "
                           "VALUES ('fixture', '123', 'completed', ?)", (str(download_root / "Sample #1"),))
        connection.execute("INSERT INTO settings VALUES ('private-fixture', 'secret-fixture-value')")
        connection.execute("PRAGMA user_version=2")
    return path


@pytest.fixture
def source(tmp_path):
    downloads = tmp_path / "source" / "downloads"
    db = database(tmp_path / "source" / "runtime" / "data" / "app.db", downloads)
    chapter = downloads / "Sample #1" / "chapter__123"
    chapter.mkdir(parents=True)
    (chapter / "00001.webp").write_bytes(b"fixture complete image")
    (chapter / "notes.txt").write_bytes(b"fixture note")
    (chapter / "empty.txt").write_bytes(b"")
    return db, downloads, tmp_path / "backups" / "sample.backup"


def test_default_is_database_plus_hash_inventory_not_resource_contents(source):
    from core.backup import create_backup, inspect_backup
    db, downloads, destination = source
    before = db.read_bytes()
    result = create_backup(db, downloads, destination)
    assert result.path == destination
    assert result.schema_version == 2 and result.database_member == "app.db"
    assert result.includes_downloads is False
    resources = result.manifest["resources"]
    assert resources["mode"] == "inventory" and resources["consistency"] == "inventory-only"
    assert resources["source_root"] == str(downloads)
    records = {record["path"]: record for record in resources["files"]}
    assert len(records) == 3
    record = records["Sample #1/chapter__123/00001.webp"]
    assert record["sha256"] == hashlib.sha256(b"fixture complete image").hexdigest()
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {"manifest.json", "app.db"}
        assert b"secret-fixture-value" not in archive.read("manifest.json")
    inspected = inspect_backup(destination)
    assert inspected.manifest == result.manifest
    assert db.read_bytes() == before
    assert list(destination.parent.iterdir()) == [destination]


def test_database_only_migration_snapshot_never_scans_downloads(source, monkeypatch):
    from core import backup
    db, _, destination = source
    monkeypatch.setattr(backup, "_inventory", lambda *args: pytest.fail("migration backup scanned resources"))
    result = backup.create_backup(db, None, destination)
    assert result.manifest["resources"] == {"mode": "none", "included": False,
                                            "consistency": "database-only", "source_root": "", "files": []}
    assert backup.inspect_backup(destination).schema_version == 2


def test_database_snapshot_records_remapping_origin_without_scanning_or_requiring_it(source, monkeypatch):
    from core import backup
    db, downloads, destination = source
    missing = downloads.parent / "absent-original-download-root"
    monkeypatch.setattr(backup, "_inventory", lambda *args: pytest.fail("database snapshot scanned resources"))
    result = backup.create_backup(db, None, destination, source_download_root=missing)
    assert result.manifest["resources"] == {"mode": "database", "included": False,
        "consistency": "database-only", "source_root": str(missing), "files": []}
    assert backup.inspect_backup(destination).manifest == result.manifest
    assert not missing.exists()
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {"manifest.json", "app.db"}


def test_source_origin_only_applies_to_database_snapshots_and_must_be_absolute(source):
    from core.backup import BackupError, create_backup
    db, downloads, destination = source
    with pytest.raises(BackupError):
        create_backup(db, downloads, destination, source_download_root=downloads)
    with pytest.raises(BackupError):
        create_backup(db, None, destination, source_download_root="relative-downloads")
    assert not destination.exists()


@pytest.mark.parametrize("schema_change", ["PRAGMA user_version=999", "PRAGMA user_version=3",
                                           "ALTER TABLE jobs ADD COLUMN unknown_field TEXT"],
                         ids=["future-version", "v3-without-catalog", "altered-v2"])
def test_ordinary_backup_requires_restore_schema_but_db_only_preserves_it(source, schema_change, monkeypatch):
    from core import backup
    db, downloads, destination = source
    backup.create_backup(db, downloads, destination)
    original_hash = hashlib.sha256(destination.read_bytes()).digest()
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute(schema_change)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    monkeypatch.setattr(backup, "_inventory", lambda *args: pytest.fail("unexpected resource scan"))
    with pytest.raises(backup.BackupError, match="数据库结构.*版本"):
        backup.create_backup(db, downloads, destination)
    assert hashlib.sha256(destination.read_bytes()).digest() == original_hash
    for mode, origin in (("none", None), ("database", downloads)):
        diagnostic = destination.with_name(mode + ".zip")
        backup.create_backup(db, None, diagnostic, source_download_root=origin)
        info = backup.inspect_backup(diagnostic)
        assert info.schema_version == version and info.manifest["resources"]["mode"] == mode


@pytest.mark.parametrize("output", ["relative/path", "{root}-old/private-title", "{root}/./private-title",
                                    "{root}/../private-title", "{root}/NUL/private-title", b"private-title"])
def test_ordinary_backup_refuses_unportable_job_paths_before_inventory(source, output, monkeypatch):
    from core import backup
    db, downloads, destination = source
    value = output.format(root=downloads) if isinstance(output, str) else output
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("UPDATE jobs SET output_path=?", (value,))
    monkeypatch.setattr(backup, "_inventory", lambda *args: pytest.fail("unexpected resource scan"))
    with pytest.raises(backup.BackupError, match=r"jobs\.output_path.*JMCONSOLE_DOWNLOAD_DIR") as failure:
        backup.create_backup(db, downloads, destination)
    assert "private-title" not in str(failure.value) and str(downloads) not in str(failure.value)
    assert list(destination.parent.iterdir()) == []


def test_unreadable_path_text_reports_the_column_without_publishing(source):
    from core.backup import BackupError, create_backup
    db, downloads, destination = source
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("UPDATE jobs SET output_path=CAST(x'ff' AS TEXT)")
    with pytest.raises(BackupError, match=r"jobs\.output_path.*文本编码"):
        create_backup(db, downloads, destination)
    assert not destination.exists()


def test_inventory_rejects_names_restore_cannot_create(source, monkeypatch):
    from core import backup
    db, downloads, destination = source
    nonportable = downloads / "private?.webp"
    if os.name == "nt":
        monkeypatch.setattr(backup, "_resource_paths", lambda *args: [nonportable])
    else:
        nonportable.write_bytes(b"fixture")
    with pytest.raises(backup.BackupError, match="资源清单.*路径名称") as failure:
        backup.create_backup(db, downloads, destination)
    assert nonportable.name not in str(failure.value) and not destination.exists()


def test_snapshot_path_scan_uses_readonly_copy_and_shared_deadline(source, monkeypatch):
    from core import backup
    db, downloads, destination = source
    backup.create_backup(db, downloads, destination)
    original_hash = hashlib.sha256(destination.read_bytes()).digest()
    open_readonly = backup._readonly_database
    clock = 0.0
    scans = []

    def observe_connection(path, budget):
        connection = open_readonly(path, budget)
        query_only = connection.execute("PRAGMA query_only").fetchone()[0]

        def expire_on_path_read(action, table, column, schema, trigger):
            nonlocal clock
            if (action, table, column) == (sqlite3.SQLITE_READ, "jobs", "output_path"):
                scans.append((path, query_only))
                clock = budget.deadline
            return sqlite3.SQLITE_OK

        connection.set_authorizer(expire_on_path_read)
        return connection

    monkeypatch.setattr(backup, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(backup, "_readonly_database", observe_connection)
    monkeypatch.setattr(backup, "_inventory", lambda *args: pytest.fail("scanned resources after timeout"))
    with pytest.raises(backup.BackupTimeout):
        backup.create_backup(db, downloads, destination, timeout=1)
    assert scans and all(destination.parent in path.parents and readonly == 1 for path, readonly in scans)
    assert hashlib.sha256(destination.read_bytes()).digest() == original_hash
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("version", [0, 1, 2])
def test_ordinary_legacy_backup_restores_empty_and_root_paths_without_catalog(source, tmp_path, version):
    from core.backup import create_backup
    from core.restore import restore_backup
    db, downloads, destination = source
    with closing(sqlite3.connect(db)) as connection, connection:
        if version < 2:
            connection.execute("DROP TABLE album_detail_cache")
            connection.execute("ALTER TABLE jobs DROP COLUMN superseded_at")
        connection.execute(f"PRAGMA user_version={version}")
        connection.executemany("INSERT INTO jobs(job_id,album_id,output_path) VALUES (?,'123',?)",
                               (("null", None), ("empty", ""), ("root", str(downloads))))
    assert create_backup(db, downloads, destination).schema_version == version
    result = restore_backup(destination, tmp_path / "restored")
    assert (result.remapped_jobs, result.remapped_resources) == (2, 0)
    with closing(sqlite3.connect(result.database)) as connection:
        assert dict(connection.execute("SELECT job_id,output_path FROM jobs")) == {
            "fixture": str(result.downloads / "Sample #1"), "null": None, "empty": "", "root": str(result.downloads)}


def test_database_snapshot_manifest_cannot_claim_a_resource_inventory(source):
    from core.backup import BackupError, create_backup, inspect_backup
    db, downloads, destination = source
    create_backup(db, None, destination, source_download_root=downloads)
    def add_inventory(name, data):
        if name == "manifest.json":
            manifest = json.loads(data)
            manifest["resources"]["files"] = [{"path": "invented.webp", "size": 1, "sha256": "0" * 64}]
            data = json.dumps(manifest).encode()
        return name, data
    rewrite(destination, add_inventory)
    with pytest.raises(BackupError, match="资源清单"):
        inspect_backup(destination)


def test_full_backup_contains_actual_resources_and_passes_stream_inspection(source):
    from core.backup import create_backup, inspect_backup
    db, downloads, destination = source
    result = create_backup(db, downloads, destination, include_downloads=True, service_stopped=True)
    assert result.includes_downloads is True
    assert result.manifest["resources"]["consistency"] == "service-stopped"
    with destination.open("rb") as stream:
        inspected = inspect_backup(stream)
        assert inspected.path is None and inspected.manifest == result.manifest
        assert not stream.closed
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            for record in inspected.manifest["resources"]["files"]:
                assert archive.read("downloads/" + record["path"]) == downloads.joinpath(*record["path"].split("/")).read_bytes()
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("status", ["queued", "paused"])
def test_stopped_service_can_back_up_waiting_or_paused_rows(source, status):
    from core.backup import create_backup
    db, downloads, destination = source
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET status=?", (status,))
    assert create_backup(db, downloads, destination, include_downloads=True, service_stopped=True).includes_downloads


@pytest.mark.parametrize("stopped", [False, None, 1])
def test_full_backup_requires_an_explicit_stopped_service_contract(source, stopped):
    from core.backup import BackupError, create_backup
    db, downloads, destination = source
    with pytest.raises(BackupError, match="服务已停止"):
        create_backup(db, downloads, destination, include_downloads=True, service_stopped=stopped)
    assert not destination.exists()


def test_running_database_is_not_a_full_resource_backup(source):
    from core.backup import BackupError, create_backup
    db, downloads, destination = source
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET status='running'")
    with pytest.raises(BackupError, match="运行中的"):
        create_backup(db, downloads, destination, include_downloads=True, service_stopped=True)
    assert not destination.exists()


def test_wal_snapshot_includes_committed_data_without_changing_source(source):
    from core.backup import create_backup
    db, _, destination = source
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO settings VALUES ('wal-fixture', 'committed')")
        connection.commit()
        create_backup(db, None, destination)
        with zipfile.ZipFile(destination) as archive:
            saved = destination.parent / "saved.db"
            saved.write_bytes(archive.read("app.db"))
        with sqlite3.connect(saved) as restored:
            assert restored.execute("SELECT value FROM settings WHERE key='wal-fixture'").fetchone() == ("committed",)
        assert connection.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 2
    finally:
        connection.close()


def test_exclusive_lock_exhausts_bounded_sqlite_backup_budget_without_publishing(source):
    from core.backup import BackupTimeout, create_backup
    db, _, destination = source
    connection = sqlite3.connect(db)
    connection.execute("BEGIN EXCLUSIVE")
    begin = time.monotonic()
    try:
        with pytest.raises(BackupTimeout):
            create_backup(db, None, destination, timeout=0.05)
    finally:
        connection.rollback()
        connection.close()
    assert time.monotonic() - begin < 1
    assert not destination.exists()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_invalid_or_exhausted_budget_is_rejected(source, timeout):
    from core.backup import BackupTimeout, create_backup
    db, downloads, destination = source
    with pytest.raises(BackupTimeout):
        create_backup(db, downloads, destination, timeout=timeout)
    assert not destination.exists()


def test_invalid_sqlite_fails_and_keeps_existing_backup(source):
    from core.backup import BackupError, create_backup
    db, _, destination = source
    destination.parent.mkdir()
    destination.write_bytes(b"previous backup")
    db.write_bytes(b"not SQLite")
    with pytest.raises(BackupError):
        create_backup(db, None, destination)
    assert destination.read_bytes() == b"previous backup"
    assert list(destination.parent.iterdir()) == [destination]


def test_staging_and_old_temporary_pages_are_not_included_or_deleted(source):
    from core.backup import create_backup
    from core.storage import STAGING_DIRECTORY
    db, downloads, destination = source
    chapter = downloads / "Sample #1" / "chapter__123"
    stage = chapter / STAGING_DIRECTORY / "attempt-old"
    stage.mkdir(parents=True)
    (stage / "partial.webp").write_bytes(b"partial")
    old = chapter / "00001.abcdefgh.webp"
    old.write_bytes(b"legacy partial")
    ordinary = chapter / "00002.foo.webp"
    ordinary.write_bytes(b"ordinary dotted image")
    result = create_backup(db, downloads, destination, include_downloads=True, service_stopped=True)
    names = {entry["path"] for entry in result.manifest["resources"]["files"]}
    assert len(names) == 4 and "Sample #1/chapter__123/00002.foo.webp" in names
    assert old.exists() and (stage / "partial.webp").exists()


def _directory_link(target, link):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_link(path):
    os.rmdir(path) if os.name == "nt" else path.unlink()


def test_resource_junction_is_rejected_instead_of_silently_omitted(source, tmp_path):
    from core.backup import BackupError, create_backup
    db, downloads, destination = source
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "foreign.webp").write_bytes(b"foreign")
    link = downloads / "redirected"
    _directory_link(outside, link)
    try:
        with pytest.raises(BackupError, match="符号链接或目录联接"):
            create_backup(db, downloads, destination, include_downloads=True, service_stopped=True)
        assert not destination.exists()
        assert (outside / "foreign.webp").read_bytes() == b"foreign"
    finally:
        _remove_link(link)


def test_destination_cannot_be_inside_downloads_or_replace_source_sidecars(source):
    from core.backup import BackupError, create_backup
    db, downloads, _ = source
    for destination in (downloads / "backup.zip", db, Path(str(db) + "-wal")):
        with pytest.raises(BackupError):
            create_backup(db, downloads, destination)


def test_destination_parent_junction_is_rejected_before_writing_outside(source, tmp_path):
    from core.backup import BackupError, create_backup
    db, downloads, _ = source
    outside, link = tmp_path / "outside", tmp_path / "redirected-backups"
    outside.mkdir()
    _directory_link(outside, link)
    try:
        with pytest.raises(BackupError):
            create_backup(db, downloads, link / "backup.zip")
        assert list(outside.iterdir()) == []
    finally:
        _remove_link(link)


def test_resource_change_during_copy_aborts_without_overwriting_previous_backup(source, monkeypatch):
    from core import backup
    db, downloads, destination = source
    destination.parent.mkdir()
    destination.write_bytes(b"previous backup")
    original = backup._hash_file
    changed = []
    def change_after_copy(path, budget, output=None):
        result = original(path, budget, output)
        if output is not None and not changed:
            changed.append(path)
            path.write_bytes(b"external change")
        return result
    monkeypatch.setattr(backup, "_hash_file", change_after_copy)
    with pytest.raises(backup.BackupError, match="发生变化"):
        backup.create_backup(db, downloads, destination, include_downloads=True, service_stopped=True)
    assert destination.read_bytes() == b"previous backup"
    assert list(destination.parent.iterdir()) == [destination]


def test_database_commit_during_full_resource_copy_invalidates_stopped_contract(source, monkeypatch):
    from core import backup
    db, downloads, destination = source
    original = backup._hash_file
    changed = []
    def change_database(path, budget, output=None):
        result = original(path, budget, output)
        if output is not None and not changed:
            changed.append(True)
            with sqlite3.connect(db) as connection:
                connection.execute("INSERT INTO settings VALUES ('external-change', 'fixture')")
        return result
    monkeypatch.setattr(backup, "_hash_file", change_database)
    with pytest.raises(backup.BackupError, match="数据库在资源备份过程中"):
        backup.create_backup(db, downloads, destination, include_downloads=True, service_stopped=True)
    assert not destination.exists()


def rewrite(path, transform, extra=()):
    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            name, data = transform(name, data)
            archive.writestr(name, data)
        for info, data in extra:
            archive.writestr(info, data)


@pytest.mark.parametrize("extra", ["../escape", "/absolute", "C:/drive", r"downloads\backslash.webp",
                                    "downloads/.jmcomic-staging/page.webp", "APP.DB"])
def test_inspection_rejects_unsafe_unexpected_and_case_conflicting_members(source, extra):
    from core.backup import BackupError, create_backup, inspect_backup
    db, downloads, destination = source
    create_backup(db, downloads, destination)
    rewrite(destination, lambda n, d: (n, d), [(extra, b"unexpected")])
    with pytest.raises(BackupError):
        inspect_backup(destination)


def test_inspection_rejects_symlink_zip_entry(source):
    from core.backup import BackupError, create_backup, inspect_backup
    db, downloads, destination = source
    create_backup(db, downloads, destination)
    symlink = zipfile.ZipInfo("downloads/link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    rewrite(destination, lambda n, d: (n, d), [(symlink, b"../../outside")])
    with pytest.raises(BackupError):
        inspect_backup(destination)


def test_inspection_reads_member_crc_and_rejects_damaged_bytes(source):
    from core.backup import BackupError, create_backup, inspect_backup
    db, downloads, destination = source
    create_backup(db, downloads, destination)
    with zipfile.ZipFile(destination) as archive:
        entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    payload = bytearray(destination.read_bytes())
    with zipfile.ZipFile(destination) as archive:
        info = archive.getinfo("app.db")
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        payload[start + 100] ^= 1
    destination.write_bytes(payload)
    with pytest.raises(BackupError):
        inspect_backup(destination)


@pytest.mark.parametrize("mutation", ["db_hash", "db_invalid", "db_schema", "inventory_hash", "mode", "version"])
def test_inspection_rejects_corrupted_or_inconsistent_manifest_and_database(source, mutation):
    from core.backup import BackupError, create_backup, inspect_backup
    db, downloads, destination = source
    create_backup(db, downloads, destination, include_downloads=True, service_stopped=True)
    def transform(name, data):
        if name == "manifest.json":
            manifest = json.loads(data)
            if mutation == "db_hash":
                manifest["database"]["sha256"] = "0" * 64
            elif mutation == "db_invalid":
                manifest["database"]["sha256"] = hashlib.sha256(b"not a database").hexdigest()
                manifest["database"]["size"] = len(b"not a database")
            elif mutation == "db_schema":
                manifest["schema_version"] += 1
            elif mutation == "inventory_hash":
                manifest["resources"]["files"][0]["sha256"] = "f" * 64
            elif mutation == "mode":
                manifest["resources"]["mode"] = "none"
            elif mutation == "version":
                manifest["version"] = 999
            data = json.dumps(manifest).encode()
        elif mutation == "db_invalid" and name == "app.db":
            data = b"not a database"
        return name, data
    rewrite(destination, transform)
    with pytest.raises(BackupError):
        inspect_backup(destination)


def test_publish_failure_preserves_previous_backup_and_cleans_temporary_files(source, monkeypatch):
    from core import backup
    db, downloads, destination = source
    destination.parent.mkdir()
    destination.write_bytes(b"previous backup")
    def refuse(*args):
        raise PermissionError("synthetic destination locked")
    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(backup.BackupError):
        backup.create_backup(db, downloads, destination)
    assert destination.read_bytes() == b"previous backup"
    assert list(destination.parent.iterdir()) == [destination]
