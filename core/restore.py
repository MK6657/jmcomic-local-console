"""Validate a backup and publish it as a new, independent application instance.

Restoration never opens the live application database or overwrites an existing
instance. All ZIP and SQLite work happens in a private sibling directory first.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile


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
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                     *(f"lpt{i}" for i in range(1, 10)), *(f"com{i}" for i in "¹²³"),
                     *(f"lpt{i}" for i in "¹²³")}
_KNOWN_TABLES = {"jobs", "settings", "search_history", "wishlist", "album_tags",
                 "album_detail_cache", "album_meta", "album_update_checks", "update_check_log",
                 "resource_catalog", "sqlite_sequence"}
_JOB_COLUMNS = {"id", "job_id", "album_id", "title", "selected_photo_ids", "status", "total_pages",
                "done_pages", "current_photo", "current_image", "output_path", "error_message",
                "created_at", "updated_at", "completed_at", "superseded_at"}
_RESOURCE_COLUMNS = {"job_id", "job_order", "album_id", "title", "selected_photo_ids", "output_path",
                     "created_at", "updated_at", "completed_at", "superseded_at"}


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


def _safe_parts(parts) -> tuple[str, ...]:
    # Keep restored data portable to Windows, including backups made on Linux.
    result = tuple(parts)
    for part in result:
        if (not part or part in (".", "..") or part.endswith((".", " "))
                or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in part)
                or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED):
            raise RestoreError("unsafe_path", "备份包含无法安全迁移的路径，目标实例未发布")
    return result


def _path_parser(source_root: str):
    if not isinstance(source_root, str) or not source_root or "\x00" in source_root:
        raise RestoreError("missing_source_root", "备份未记录源下载根目录，不能自动恢复到新实例")
    windows = PureWindowsPath(source_root)
    parser = PureWindowsPath if windows.is_absolute() else PurePosixPath
    root = parser(source_root)
    if not root.is_absolute() or any(part in (".", "..") for part in source_root.replace("\\", "/").split("/")):
        raise RestoreError("unsafe_path", "备份的源下载根目录无效，目标实例未发布")
    _safe_parts(root.parts[1:])
    return parser, root


def _map_output(value, parser, source_root, destination: Path):
    if value is None or value == "":
        return value
    if not isinstance(value, str) or "\x00" in value:
        raise RestoreError("unmapped_path", "备份存在不能映射的下载路径，目标实例未发布")
    # PurePath normalizes dot components, so reject them in the original spelling.
    if any(part in (".", "..") for part in value.replace("\\", "/").split("/")):
        raise RestoreError("unmapped_path", "备份存在不能映射的下载路径，目标实例未发布")
    path = parser(value)
    try:
        if not path.is_absolute():
            raise ValueError()
        relative = path.relative_to(source_root)
    except ValueError:
        raise RestoreError("unmapped_path", "备份包含源下载根目录之外的路径，目标实例未发布") from None
    return str(destination.joinpath(*_safe_parts(relative.parts)))


def _normal_sql(value: str) -> str:
    # Preserve literal case: 'COMPLETED' would change a trigger's comparison.
    return re.sub(r"\s+", " ", value).strip()


def _known_catalog_triggers() -> dict[str, str]:
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


def _validate_schema(conn, manifest_version: int) -> dict[str, str]:
    from .database import CURRENT_SCHEMA_VERSION
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != manifest_version or not 0 <= version <= CURRENT_SCHEMA_VERSION:
        raise RestoreError("unsupported_schema", "备份数据库版本不受当前程序支持，目标实例未发布")
    objects = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    tables = {row[1] for row in objects if row[0] == "table"}
    if not {"jobs", "settings"} <= tables or not tables <= _KNOWN_TABLES:
        raise RestoreError("unsupported_schema", "备份数据库结构不受当前程序支持，目标实例未发布")
    if any(row[0] == "view" or (row[0] == "table" and "virtual table" in row[2].casefold()) for row in objects):
        raise RestoreError("unsupported_schema", "备份数据库结构不受当前程序支持，目标实例未发布")
    checks = {"jobs": ({"job_id", "album_id", "status", "output_path"}, _JOB_COLUMNS),
              "settings": ({"key", "value"}, {"key", "value"})}
    if version >= 3:
        if "resource_catalog" not in tables:
            raise RestoreError("unsupported_schema", "备份资源目录结构不完整，目标实例未发布")
        checks["resource_catalog"] = ({"job_id", "output_path"}, _RESOURCE_COLUMNS)
    elif "resource_catalog" in tables:
        raise RestoreError("unsupported_schema", "备份资源目录版本不一致，目标实例未发布")
    for table, (required, allowed) in checks.items():
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not required <= columns or not columns <= allowed:
            raise RestoreError("unsupported_schema", "备份数据库字段不受当前程序支持，目标实例未发布")
    triggers = {row[1]: row[2] for row in objects if row[0] == "trigger"}
    known = _known_catalog_triggers() if version >= 3 else {}
    if triggers.keys() != known.keys() or any(_normal_sql(sql) != _normal_sql(known[name]) for name, sql in triggers.items()):
        raise RestoreError("unsupported_schema", "备份数据库触发器不受当前程序支持，目标实例未发布")
    return triggers


def _rewrite_database(path: Path, manifest: dict, final_downloads: Path, deadline: float) -> tuple[int, int, int]:
    parser, source = _path_parser(manifest["resources"]["source_root"])
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        triggers = _validate_schema(connection, manifest["schema_version"])
        connection.execute("BEGIN IMMEDIATE")
        # Never let UPDATE triggers re-copy catalog metadata from task history.
        # Their exact known definitions were checked above and are reinstalled.
        for name in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        counts = []
        for table in ("jobs", "resource_catalog"):
            if table == "resource_catalog" and manifest["schema_version"] < 3:
                counts.append(0)
                continue
            count = 0
            for job_id, output in connection.execute(f"SELECT job_id, output_path FROM {table}").fetchall():
                _check_deadline(deadline)
                mapped = _map_output(output, parser, source, final_downloads)
                if output not in (None, ""):
                    connection.execute(f"UPDATE {table} SET output_path=? WHERE job_id=?", (mapped, job_id))
                    count += 1
            counts.append(count)
        connection.execute("INSERT INTO settings(key, value) VALUES ('download_root', ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(final_downloads),))
        requeued = connection.execute("UPDATE jobs SET status='queued' WHERE status='running'").rowcount
        for sql in triggers.values():
            connection.execute(sql)
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RestoreError("invalid_database", "恢复后的数据库校验失败，目标实例未发布")
        return counts[0], counts[1], requeued
    finally:
        connection.close()


def _private_parent(parent: Path, root: Path, identities: dict[Path, tuple]) -> None:
    current = root
    _check_chain(identities)
    for component in parent.relative_to(root).parts:
        current = current / component
        try:
            current.mkdir()
        except FileExistsError:
            pass
        identity = _plain_directory(current)
        if current in identities and identities[current] != identity:
            raise RestoreError("unsafe_target", "恢复暂存目录发生变化，目标实例未发布")
        identities[current] = identity


def _copy_member(archive, name: str, target: Path, expected: dict, deadline: float) -> None:
    digest = hashlib.sha256()
    size = 0
    with archive.open(name) as source, target.open("xb") as output:
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
    from .backup import BackupError, inspect_backup
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
        _safe_parts((target.name,))
        chain = _directory_chain(target.parent)
        _check_deadline(deadline)
        with Path(archive_path).open("rb") as handle:
            info = inspect_backup(handle, timeout=max(0.0, deadline - time.monotonic()))
            manifest = info.manifest
            _path_parser(manifest["resources"]["source_root"])
            for entry in manifest["resources"]["files"]:
                _safe_parts(entry["path"].split("/"))
            _check_chain(chain)
            stage = Path(tempfile.mkdtemp(prefix=".jmcomic-restore-", dir=target.parent))
            stage_identity = _plain_directory(stage)
            private_directories[stage] = stage_identity
            database = stage / "runtime" / "data" / "app.db"
            downloads = stage / "downloads"
            _private_parent(downloads, stage, private_directories)
            _private_parent(database.parent, stage, private_directories)
            handle.seek(0)
            with zipfile.ZipFile(handle) as archive:
                _copy_member(archive, "app.db", database, manifest["database"], deadline)
                # Validate schema and all output paths before extracting resources.
                jobs, resources, requeued = _rewrite_database(database, manifest, target / "downloads", deadline)
                restored = 0
                if manifest["resources"]["included"]:
                    for entry in manifest["resources"]["files"]:
                        parts = _safe_parts(entry["path"].split("/"))
                        output = downloads.joinpath(*parts)
                        _check_chain(chain)
                        _private_parent(output.parent, stage, private_directories)
                        _copy_member(archive, "downloads/" + entry["path"], output, entry, deadline)
                        restored += 1
            _check_deadline(deadline)
            _check_chain(chain)
            _check_chain(private_directories)
            if os.path.lexists(target):
                raise RestoreError("target_exists", "恢复目标已存在，请指定一个全新的实例目录")
            if _plain_directory(stage) != stage_identity:
                raise RestoreError("unsafe_target", "恢复暂存目录发生变化，目标实例未发布")
            _publish_new_directory(stage, target)
            stage = None
            included = manifest["resources"]["included"]
            warnings = () if included else ("此备份未包含资源文件；仅恢复数据库与目录记录，本地阅读需另行恢复资源文件。",)
            return RestoreResult(target, target / "runtime" / "data" / "app.db", target / "downloads",
                                 included, restored, jobs, resources, requeued, warnings)
    except RestoreError:
        raise
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
