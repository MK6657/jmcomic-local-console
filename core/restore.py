"""Validate a backup and publish it as a new, independent application instance.

Restoration never opens the live application database or overwrites an existing
instance. All ZIP and SQLite work happens in a private sibling directory first.
"""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePath, PureWindowsPath
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile

from .backup_paths import BackupPathError, output_parts, output_path_tables, parse_source_root, safe_parts


class RestoreError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class RestoreTimeout(RestoreError, TimeoutError):
    def __init__(self):
        super().__init__("timeout", "恢复操作超时，目标实例未发布")


@dataclass(frozen=True)
class RestoreResult:
    target: Path
    database: Path
    downloads: Path
    resources_included: bool
    restored_files: int
    remapped_jobs: int
    remapped_resources: int
    requeued_jobs: int
    warnings: tuple[str, ...] = ()


_REPARSE_POINT = 0x400


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise RestoreTimeout()


def _plain_directory(path: Path) -> tuple:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
        raise RestoreError("unsafe_target", "恢复目标的父目录不能包含符号链接或目录联接")
    return info.st_dev, info.st_ino


def _directory_chain(path: Path) -> dict[Path, tuple]:
    return {parent: _plain_directory(parent) for parent in reversed((path, *path.parents))}


def _check_chain(chain: dict[Path, tuple]) -> None:
    if any(_plain_directory(path) != identity for path, identity in chain.items()):
        raise RestoreError("unsafe_target", "恢复期间目标父目录发生变化，目标实例未发布")


def _validate_schema(conn, manifest_version: int, deadline: float) -> dict[str, str]:
    from .database_schema import validate_schema, SchemaError
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != manifest_version:
        raise RestoreError("unsupported_schema", "备份数据库版本与清单不一致，目标实例未发布")
    try:
        return validate_schema(conn, version)
    except (SchemaError, sqlite3.Error):
        _check_deadline(deadline)
        raise RestoreError("unsupported_schema", "备份数据库结构不受当前程序支持，目标实例未发布") from None


def _rewrite_database(path: Path, version: int, source_root: PurePath, final_downloads: Path,
                      deadline: float, include_downloads: bool = False) -> tuple[int, int, int]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        triggers = _validate_schema(connection, version, deadline)
        connection.execute("BEGIN IMMEDIATE")
        # Never let UPDATE triggers re-copy catalog metadata from task history.
        # Their exact known definitions were checked above and are reinstalled.
        for name in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        counts = {"jobs": 0, "resource_catalog": 0}
        for table in output_path_tables(version):
            for row_id, output in connection.execute(f"SELECT rowid, output_path FROM {table}").fetchall():
                _check_deadline(deadline)
                parts = output_parts(table, output, source_root)
                if parts is not None:
                    # SQLite permits NULL in TEXT PRIMARY KEY columns. Use the
                    # validated ordinary table's row identity, including those rows.
                    changed = connection.execute(f"UPDATE {table} SET output_path=? WHERE rowid=?",
                                                 (str(final_downloads.joinpath(*parts)), row_id)).rowcount
                    if changed != 1:
                        raise RestoreError("invalid_database", "恢复路径映射失败，目标实例未发布")
                    counts[table] += 1
        connection.execute("INSERT INTO settings(key, value) VALUES ('download_root', ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(final_downloads),))
        if version >= 4:
            from .task_commands import queue_tail_time
            interrupted = connection.execute(
                "SELECT job_id FROM jobs WHERE status='running' ORDER BY created_at, id",
            ).fetchall()
            for (job_id,) in interrupted:
                _check_deadline(deadline)
                connection.execute("UPDATE jobs SET status='queued', queued_at=? WHERE job_id=?",
                                   (queue_tail_time(connection), job_id))
            requeued = len(interrupted)
        else:
            requeued = connection.execute("UPDATE jobs SET status='queued' WHERE status='running'").rowcount
        if version >= 5 and not include_downloads:
            connection.execute("DELETE FROM task_page_receipts")
            connection.execute("DELETE FROM published_tasks")
            connection.execute(
                "UPDATE album_maintenance SET state=CASE WHEN state='running' THEN 'pending' ELSE state END, "
                "payload=CASE WHEN json_valid(payload) THEN json_remove(payload, '$._move') ELSE payload END"
            )
        for sql in triggers.values():
            connection.execute(sql)
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RestoreError("invalid_database", "恢复后的数据库校验失败，目标实例未发布")
        return counts["jobs"], counts["resource_catalog"], requeued
    finally:
        connection.close()


def _private_parent(parent: Path, root: Path, identities: dict[Path, tuple]) -> None:
    current = root
    if _plain_directory(root) != identities[root]:
        raise RestoreError("unsafe_target", "恢复暂存目录发生变化，目标实例未发布")
    for component in parent.relative_to(root).parts:
        current = current / component
        if current not in identities:
            try:
                current.mkdir()
            except FileExistsError:
                pass
        identity = _plain_directory(current)
        if current in identities and identities[current] != identity:
            raise RestoreError("unsafe_target", "恢复暂存目录发生变化，目标实例未发布")
        identities[current] = identity


def _restore_move_plan(database, manifest, deadline):
    from .backup import _maintenance_moves, _move_placements

    def check():
        _check_deadline(deadline)

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        moves = list(_maintenance_moves(connection, manifest["schema_version"], check))
    phases = manifest["resources"].get("maintenance_moves")
    if phases is None and moves:
        raise RestoreError("missing_move_evidence", "此旧版备份包含中断的目录整理且缺少位置记录，目标实例未发布")
    phases = phases or {}
    return moves, phases, _move_placements(moves, phases, check)


def _finish_restored_moves(database, version, final_downloads, staged_downloads, identities, moves, phases, deadline):
    """Rebind unfinished moves and commit already-moved catalog paths before publishing."""
    if not moves:
        return
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        triggers = _validate_schema(connection, version, deadline)
        connection.execute("BEGIN IMMEDIATE")
        for name in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        for move in moves:
            _check_deadline(deadline)
            payload = move.payload
            if phases[move.album_id] == "at_target":
                source, target = (final_downloads.joinpath(*parts) for parts in (move.source, move.target))
                source_norm = os.path.normcase(os.path.abspath(source))
                matched_jobs = [
                    row[0] for row in connection.execute(
                        "SELECT p.job_id, c.output_path FROM published_tasks p JOIN resource_catalog c USING(job_id) "
                        "WHERE p.album_id=? AND c.superseded_at IS NULL AND c.output_path IS NOT NULL",
                        (move.album_id,),
                    ).fetchall()
                    if row[1] and os.path.normcase(os.path.abspath(row[1])) == source_norm
                ]
                if matched_jobs:
                    placeholders = ",".join("?" for _ in matched_jobs)
                    connection.execute(
                        f"UPDATE jobs SET output_path=? WHERE album_id=? AND status='completed' AND job_id IN ({placeholders})",
                        (str(target), move.album_id, *matched_jobs),
                    )
                for table in output_path_tables(version):
                    for (path,) in connection.execute(f"SELECT DISTINCT output_path FROM {table} WHERE album_id=?",
                                                      (move.album_id,)).fetchall():
                        _check_deadline(deadline)
                        if path and os.path.normcase(os.path.abspath(path)) == source_norm:
                            connection.execute(f"UPDATE {table} SET output_path=? WHERE album_id=? AND output_path=?",
                                               (str(target), move.album_id, path))
                relative = "/".join(move.target)
                payload.update(directory=relative, preferred_directory=relative)
                del payload["_move"]
            else:
                intent = payload["_move"]
                intent["identity"] = list(identities[staged_downloads.joinpath(*move.source)])
                if move.retained:
                    intent["old_identity"] = list(identities[staged_downloads.joinpath(*move.target)])
            connection.execute("UPDATE album_maintenance SET payload=? WHERE album_id=?",
                               (json.dumps(payload, ensure_ascii=False), move.album_id))
        for sql in triggers.values():
            connection.execute(sql)
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RestoreError("invalid_database", "恢复后的整理记录校验失败，目标实例未发布")


def _copy_member(archive, name: str, target: Path, expected: dict, deadline: float) -> None:
    import struct
    digest = hashlib.sha256()
    size = 0
    info = archive.getinfo(name)
    with archive.open(info) as source, target.open("xb") as output:
        while True:
            _check_deadline(deadline)
            block = source.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > expected["size"]:
                raise RestoreError("invalid_backup", "备份内容与清单不一致，目标实例未发布")
            digest.update(block)
            output.write(block)
    if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
        raise RestoreError("invalid_backup", "备份内容与清单不一致，目标实例未发布")
    if name.startswith("downloads/"):
        try:
            if len(info.extra) >= 12:
                tag, length, mtime_ns = struct.unpack("<HHQ", info.extra[:12])
                if tag == 0x4A4D and length == 8 and mtime_ns > 0:
                    os.utime(target, ns=(mtime_ns, mtime_ns))
                    return
            if info.date_time > (1980, 1, 1, 0, 0, 0):
                ts = time.mktime((*info.date_time, 0, 0, -1))
                os.utime(target, (ts, ts))
        except (OSError, OverflowError, ValueError):
            pass


def _publish_new_directory(stage: Path, target: Path) -> None:
    if os.name == "nt":
        try:
            os.rename(stage, target)  # Windows never replaces an existing directory.
        except FileExistsError:
            raise RestoreError("target_exists", "恢复目标已存在，请指定一个全新的实例目录") from None
        return
    if sys.platform.startswith("linux"):
        # POSIX rename may replace an empty destination directory. Linux's
        # no-replace operation also protects against a target created after our
        # last existence check; do not fall back to an overwriting rename.
        import ctypes
        import errno
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise RestoreError("unsupported_platform", "系统不支持安全发布新目录，目标实例未发布")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1) == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise RestoreError("target_exists", "恢复目标已存在，请指定一个全新的实例目录")
        raise OSError(error, "directory publication failed")
    raise RestoreError("unsupported_platform", "系统不支持安全发布新目录，目标实例未发布")


def restore_backup(archive_path, target_dir, *, timeout: float = 120) -> RestoreResult:
    """Restore only to a nonexistent absolute instance directory; never overwrite.

    A database/inventory backup intentionally produces an empty downloads folder;
    returned warnings explicitly state that resources were not part of the backup.
    """
    from .backup import BackupError, _move_resource_path, _open_backup_zip, inspect_backup
    target = Path(target_dir)
    if not target.is_absolute():
        raise RestoreError("unsafe_target", "恢复目标必须是一个全新的绝对目录")
    target = Path(os.path.abspath(target))
    if os.path.lexists(target):
        raise RestoreError("target_exists", "恢复目标已存在，请指定一个全新的实例目录")
    stage = None
    stage_identity = None
    private_directories = {}
    chain = None
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        safe_parts((target.name,))
        chain = _directory_chain(target.parent)
        _check_deadline(deadline)
        with Path(archive_path).open("rb") as handle:
            info = inspect_backup(handle, timeout=max(0.0, deadline - time.monotonic()))
            manifest = info.manifest
            source_root = parse_source_root(manifest["resources"]["source_root"])
            source_windows = isinstance(source_root, PureWindowsPath)
            for entry in manifest["resources"]["files"]:
                _check_deadline(deadline)
                safe_parts(entry["path"].split("/"))
            _check_chain(chain)
            stage = Path(tempfile.mkdtemp(prefix=".jmcomic-restore-", dir=target.parent))
            stage_identity = _plain_directory(stage)
            private_directories[stage] = stage_identity
            database = stage / "runtime" / "data" / "app.db"
            downloads = stage / "downloads"
            _private_parent(downloads, stage, private_directories)
            _private_parent(database.parent, stage, private_directories)
            handle.seek(0)
            with _open_backup_zip(handle, lambda: _check_deadline(deadline)) as archive:
                _copy_member(archive, "app.db", database, manifest["database"], deadline)
                # Validate schema and all output paths before extracting resources.
                jobs, resources, requeued = _rewrite_database(
                    database, manifest["schema_version"], source_root,
                    target / "downloads", deadline,
                    bool(manifest["resources"]["included"]),
                )
                restored = 0
                if manifest["resources"]["included"]:
                    _check_chain(chain)
                    _private_parent(database.parent, stage, private_directories)
                    moves, phases, placements = _restore_move_plan(database, manifest, deadline)
                    for entry in manifest["resources"]["files"]:
                        parts = safe_parts(entry["path"].split("/"))
                        output = downloads.joinpath(*_move_resource_path(parts, placements, source_windows=source_windows))
                        _check_chain(chain)
                        _private_parent(output.parent, stage, private_directories)
                        _copy_member(archive, "downloads/" + entry["path"], output, entry, deadline)
                        restored += 1
                    if placements:
                        _check_chain(chain)
                        for _, parts in placements.values():
                            _check_deadline(deadline)
                            if parts is not None:
                                _private_parent(downloads.joinpath(*parts), stage, private_directories)
                        _private_parent(database.parent, stage, private_directories)
                        _finish_restored_moves(database, manifest["schema_version"], target / "downloads", downloads,
                                               private_directories, moves, phases, deadline)
            _check_deadline(deadline)
            _check_chain(chain)
            _check_chain(private_directories)
            if os.path.lexists(target):
                raise RestoreError("target_exists", "恢复目标已存在，请指定一个全新的实例目录")
            if _plain_directory(stage) != stage_identity:
                raise RestoreError("unsafe_target", "恢复暂存目录发生变化，目标实例未发布")
            _check_deadline(deadline)
            _publish_new_directory(stage, target)
            stage = None
            included = manifest["resources"]["included"]
            warnings = () if included else ("此备份未包含资源文件；仅恢复数据库与目录记录，本地阅读需另行恢复资源文件。",)
            return RestoreResult(target, target / "runtime" / "data" / "app.db", target / "downloads",
                                 included, restored, jobs, resources, requeued, warnings)
    except RestoreError:
        raise
    except BackupPathError as error:
        raise RestoreError(error.reason, f"{error}，目标实例未发布") from None
    except BackupError as error:
        if isinstance(error, TimeoutError):
            raise RestoreTimeout() from None
        raise RestoreError("invalid_backup", "备份校验失败，目标实例未发布") from None
    except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, KeyError, TypeError):
        if time.monotonic() >= deadline:
            raise RestoreTimeout() from None
        raise RestoreError("restore_failed", "恢复失败，目标实例未发布，请检查备份、目录权限与磁盘空间") from None
    finally:
        if stage is not None:
            try:
                # Delete only the private directory created in this invocation,
                # and never follow a changed parent or redirected stage root.
                if chain is not None:
                    _check_chain(chain)
                _check_chain(private_directories)
                if _plain_directory(stage) != stage_identity:
                    raise RestoreError("unsafe_target", "恢复暂存目录已更换")
                shutil.rmtree(stage)
            except (OSError, RestoreError):
                pass
