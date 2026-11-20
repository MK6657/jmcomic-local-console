"""Versioned private backups, independent of application/SDK initialization.

The database uses SQLite's online backup API. Default resource inventories contain
hashes, not file contents. Including resources requires an explicit stopped-service
contract held by the caller (normally the application's maintenance mutex); hashes
are rechecked, but this is not a cross-filesystem snapshot against external writers.
Backups contain private database data and belong in a private, ignored directory.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
import zipfile
import zlib
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .backup_paths import BackupPathError, output_parts, output_path_tables, parse_source_root, safe_parts
from .database_schema import SchemaError, validate_schema
from .file_tree import is_link
from .storage import STAGING_DIRECTORY, is_temporary_path, staged_file
from .validation import validate_numeric
from .zip_preflight import BoundedZipFile, ZipMetadataLimit

FORMAT = "jmcomic-local-console-backup"
FORMAT_VERSION = 1
_MOVE_FORMAT_VERSION = 2
DATABASE_MEMBER = "app.db"
MANIFEST_MEMBER = "manifest.json"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024
MAX_DATABASE_BYTES = 2 * 1024**3
MAX_RESOURCE_BYTES = 32 * 1024**3
MAX_TOTAL_BYTES = 1024**4
MAX_FILES = 100_000
_CHUNK = 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class BackupError(ValueError):
    """No complete, validated backup was produced/read."""


class BackupTimeout(BackupError, TimeoutError):
    """The shared backup/validation time budget was exhausted."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path | None
    manifest: dict

    @property
    def schema_version(self):
        return self.manifest["schema_version"]

    @property
    def database_member(self):
        return self.manifest["database"]["path"]

    @property
    def includes_downloads(self):
        return self.manifest["resources"]["included"]


@dataclass(frozen=True)
class _MaintenanceMove:
    album_id: str
    payload: dict
    source: tuple[str, ...]
    target: tuple[str, ...]
    retained: tuple[str, ...] | None
    identity: tuple[int, int]
    old_identity: tuple[int, int] | None

    def placements(self, phase):
        """Map each archived directory to its restored location, or None if absent."""
        if phase == "at_target":
            locations = (None, self.target, self.retained)
        elif phase == "old_retained" and self.retained:
            locations = (self.source, None, self.target)
        elif phase == "at_source":
            locations = (self.source, self.target if self.retained else None, None)
        else:
            raise BackupError("备份中的目录整理阶段与记录不一致")
        return ((parts, location) for parts, location in zip((self.source, self.target, self.retained), locations)
                if parts is not None)


_MOVE_PHASES = {"at_source", "old_retained", "at_target"}


class _Budget:
    def __init__(self, timeout):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise BackupTimeout("备份时间预算必须大于零")
        self.deadline = time.monotonic() + timeout

    def check(self):
        if time.monotonic() >= self.deadline:
            raise BackupTimeout("备份或校验超过时间预算，未发布新备份")

    def remaining(self):
        self.check()
        return max(0.001, self.deadline - time.monotonic())


def _absolute(path):
    return Path(os.path.abspath(path))


def _plain_path(path, *, directory=False):
    """Reject every redirecting ancestor, including junctions contained in the tree."""
    for parent in reversed(path.parents):
        if is_link(parent) or not parent.is_dir():
            raise BackupError("备份路径不能经过符号链接或目录联接")
    info = path.lstat()
    if is_link(path) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise BackupError("备份路径必须是普通目录或文件，不能是链接或特殊文件")
    return info


def _ensure_directory(path):
    for part in reversed((path, *path.parents)):
        if not part.exists():
            try:
                part.mkdir()
            except FileExistsError:
                pass
        _plain_path(part, directory=True)


def _inside(path, root):
    return path == root or root in path.parents


def _relative_name(name):
    if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
            or name.startswith("/") or ":" in name
            or any(part in ("", ".", "..") for part in name.split("/"))
            or PurePosixPath(name).is_absolute() or is_temporary_path(name)):
        raise BackupError("备份包含不安全的文件名或暂存路径")
    return name


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _resource_paths(root, budget):
    """Strict traversal: fail on links/unreadable content instead of silently losing it."""
    _plain_path(root, directory=True)
    files, pending = [], [root]
    while pending:
        budget.check()
        folder = pending.pop()
        _plain_path(folder, directory=True)
        with os.scandir(folder) as entries:
            for entry in entries:
                budget.check()
                path = Path(entry.path)
                if is_link(path):
                    raise BackupError("资源目录包含符号链接或目录联接，不能建立完整清单")
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() != STAGING_DIRECTORY:
                        pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    if not is_temporary_path(entry.name):
                        _relative_name(path.relative_to(root).as_posix())
                        files.append(path)
                        if len(files) > MAX_FILES:
                            raise BackupError("备份资源文件数量超过上限")
                else:
                    raise BackupError("资源目录包含不支持的特殊文件")
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def _hash_file(path, budget, output=None):
    before = _plain_path(path)
    digest, size = hashlib.sha256(), 0
    # O_NOFOLLOW is unavailable on Windows; repeat the lstat/handle identity check
    # there. The caller's stopped-service contract excludes our own file writers.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        if _signature(os.fstat(source.fileno())) != _signature(before):
            raise BackupError("资源文件在读取前被替换")
        while True:
            budget.check()
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESOURCE_BYTES:
                raise BackupError("单个资源文件超过备份大小上限")
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        after = os.fstat(source.fileno())
    if _signature(before) != _signature(after) or _signature(before) != _signature(_plain_path(path)):
        raise BackupError("资源文件在备份过程中发生变化")
    return size, digest.hexdigest()


def _inventory(root, budget):
    result, names, total = [], set(), 0
    for path in _resource_paths(root, budget):
        relative = path.relative_to(root)
        safe_parts(relative.parts, context="资源清单")
        name = relative.as_posix()
        if name.casefold() in names:
            raise BackupError("资源文件名大小写冲突，无法跨平台恢复")
        names.add(name.casefold())
        size, digest = _hash_file(path, budget)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise BackupError("备份资源总大小超过上限")
        result.append({"path": name, "size": size, "sha256": digest})
    return result


def _readonly_database(path, budget):
    _plain_path(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                 timeout=min(0.1, budget.remaining()))
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.set_progress_handler(lambda: int(time.monotonic() >= budget.deadline), 1000)
    return connection


def _check_database(path, budget):
    budget.check()
    with closing(_readonly_database(path, budget)) as connection:
        rows = connection.execute("PRAGMA quick_check").fetchall()
        budget.check()
        if rows != [("ok",)]:
            raise BackupError("SQLite 快照完整性检查失败")
        return connection.execute("PRAGMA user_version").fetchone()[0]


def _snapshot(source, destination, budget):
    page_size = source.execute("PRAGMA page_size").fetchone()[0]
    def progress(status, remaining, total):
        budget.check()
        if total * page_size > MAX_DATABASE_BYTES:
            raise BackupError("数据库超过备份大小上限")
    budget.check()
    with closing(sqlite3.connect(destination, timeout=min(0.1, budget.remaining()))) as target:
        source.backup(target, pages=128, progress=progress, sleep=0.01)
    os.chmod(destination, 0o600)
    if destination.stat().st_size > MAX_DATABASE_BYTES:
        raise BackupError("数据库超过备份大小上限")
    return _check_database(destination, budget)


def _has_running_jobs(connection):
    return connection.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone() is not None


def _move_parts(value):
    if not isinstance(value, str) or is_temporary_path(value):
        raise BackupError("备份中的目录整理记录无效")
    parts = safe_parts(value.replace("\\", "/").split("/"), context="目录整理记录")
    if any(part.casefold() == ".jmcomic-work" for part in parts):
        raise BackupError("备份中的目录整理指向私有任务目录")
    return parts


def _move_identity(value):
    if (not isinstance(value, list) or len(value) != 2 or any(type(item) is not int for item in value)
            or value[0] < 0 or value[1] <= 0):
        raise BackupError("备份中的目录整理缺少文件系统标识")
    return tuple(value)


def _maintenance_moves(connection, version, check):
    """Read durable move intents from the validated snapshot without loading the app."""
    if version < 5:
        return
    rows = connection.execute("SELECT album_id,payload FROM album_maintenance WHERE CASE WHEN json_valid(payload) "
                              "THEN json_type(payload,'$._move')='object' ELSE 0 END")
    for album_id, raw in rows:
        check()
        payload = json.loads(raw)
        intent = payload["_move"]
        if not validate_numeric(album_id) or ("displaced" in intent) != ("old_identity" in intent):
            raise BackupError("备份中的目录整理记录无效")
        try:
            source, target = _move_parts(payload["directory"]), _move_parts(intent["to"])
            retained = _move_parts(intent["displaced"]) if "displaced" in intent else None
            old_identity = _move_identity(intent["old_identity"]) if retained else None
            identity = _move_identity(intent["identity"])
        except KeyError:
            raise BackupError("备份中的目录整理记录不完整") from None
        yield _MaintenanceMove(album_id, payload, source, target, retained, identity, old_identity)


def _move_placements(moves, phases, check):
    if {move.album_id for move in moves} != phases.keys():
        raise BackupError("备份中的目录整理阶段与数据库不一致")
    placements = {}
    for move in moves:
        check()
        for parts, location in move.placements(phases[move.album_id]):
            key = PureWindowsPath(*parts)
            if key in placements:
                raise BackupError("备份中的目录整理路径重叠")
            placements[key] = (parts, location)
    for path in placements:
        check()
        if any(parent in placements for parent in path.parents):
            raise BackupError("备份中的目录整理路径重叠")
    return placements


def _move_resource_path(parts, placements, *, source_windows):
    if not placements:
        return parts
    key = PureWindowsPath(*parts)
    if key in placements:
        raise BackupError("目录整理路径与文件清单冲突")
    for depth, parent in enumerate(key.parents, start=1):
        if parent in placements:
            original, location = placements[parent]
            if location is None or (not source_windows and original != parts[:-depth]):
                raise BackupError("目录整理阶段与文件清单冲突")
            return (*location, *parts[-depth:])
    return parts


def _move_phase(root, move):
    def identity(parts):
        if parts is None:
            return None
        try:
            info = _plain_path(root.joinpath(*parts), directory=True)
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino

    observed = tuple(identity(parts) for parts in (move.source, move.target, move.retained))
    if observed == (move.identity, move.old_identity, None):
        return "at_source"
    if move.retained and observed == (move.identity, None, move.old_identity):
        return "old_retained"
    if observed == (None, move.identity, move.old_identity):
        return "at_target"
    raise BackupError("无法确认中断整理的目录位置；请先恢复整理或检查资源后重新备份")


def _check_restorable_snapshot(connection, version, root, budget):
    budget.check()
    try:
        validate_schema(connection, version)
    except (SchemaError, sqlite3.Error):
        budget.check()
        raise BackupError("数据库结构不受当前恢复程序支持，未发布新备份；请核对数据库来源并使用匹配的程序版本重试") from None
    source = parse_source_root(str(root))
    for table in output_path_tables(version):
        budget.check()
        try:
            for (output,) in connection.execute(f"SELECT output_path FROM {table}"):
                budget.check()
                output_parts(table, output, source)
        except sqlite3.Error:
            budget.check()
            raise BackupError(f"无法读取 {table}.output_path，未发布新备份；请检查路径记录的文本编码和数据库完整性后重试") from None
    budget.check()


def create_backup(database_path, download_root, destination, *, include_downloads=False,
                  service_stopped=False, source_download_root=None, timeout=120) -> BackupInfo:
    """Create and validate one ZIP, publishing atomically only on complete success.

    ``download_root=None`` creates a database-only migration snapshot without any
    resource scan. ``source_download_root`` can record an absolute remapping origin
    for that snapshot without inspecting or requiring the directory to exist.
    A normal backup requires a restorable schema and portable paths. It records
    a hash inventory; including file bytes requires ``include_downloads=True``
    and the caller-held stopped-service contract.
    """
    budget = _Budget(timeout)
    database_path, destination = _absolute(database_path), _absolute(destination)
    root = None if download_root is None else _absolute(download_root)
    source_root = root
    if source_download_root is not None:
        if root is not None or not Path(source_download_root).is_absolute():
            raise BackupError("仅数据库快照的资源来源必须是绝对目录，且不能同时扫描下载目录")
        source_root = _absolute(source_download_root)
    if type(include_downloads) is not bool:
        raise BackupError("是否包含资源文件必须明确为布尔值")
    if include_downloads and (root is None or service_stopped is not True):
        raise BackupError("包含资源文件的备份要求下载目录及明确的服务已停止契约")
    if (destination in (database_path, Path(str(database_path) + "-wal"), Path(str(database_path) + "-shm"),
                        Path(str(database_path) + "-journal"))
            or (source_root is not None and _inside(destination, source_root))):
        raise BackupError("备份目标不能覆盖源数据库或位于下载目录内部")
    if root is not None and _inside(database_path, root):
        raise BackupError("源数据库不能位于要备份的下载目录内部")
    try:
        _plain_path(database_path)
        if root is not None:
            _plain_path(root, directory=True)
        _ensure_directory(destination.parent)
        with staged_file(destination, suffix=".zip") as staged, tempfile.TemporaryDirectory(
                prefix="jm-backup-", dir=staged.path.parent) as work:
            snapshot = Path(work) / DATABASE_MEMBER
            with closing(_readonly_database(database_path, budget)) as source:
                data_version = source.execute("PRAGMA data_version").fetchone()[0]
                schema = _snapshot(source, snapshot, budget)
                moves = []
                if root is not None:
                    with closing(_readonly_database(snapshot, budget)) as saved:
                        _check_restorable_snapshot(saved, schema, root, budget)
                        if include_downloads and _has_running_jobs(saved):
                            raise BackupError("数据库仍有运行中的下载任务，不能确认资源备份已停机")
                        if include_downloads:
                            moves = list(_maintenance_moves(saved, schema, budget.check))
                phases = {}
                for move in moves:
                    budget.check()
                    phases[move.album_id] = _move_phase(root, move)
                placements = _move_placements(moves, phases, budget.check)
                inventory = [] if root is None else _inventory(root, budget)
                if placements:
                    source_windows = isinstance(parse_source_root(str(root)), PureWindowsPath)
                    for record in inventory:
                        budget.check()
                        _move_resource_path(tuple(record["path"].split("/")), placements, source_windows=source_windows)
                db_size, db_hash = _hash_file(snapshot, budget)
                mode = (("none" if source_root is None else "database") if root is None
                        else ("full" if include_downloads else "inventory"))
                manifest = {
                    "format": FORMAT, "version": FORMAT_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(), "schema_version": schema,
                    "database": {"path": DATABASE_MEMBER, "size": db_size, "sha256": db_hash},
                    "resources": {"mode": mode, "included": include_downloads,
                                  "consistency": {"none": "database-only", "database": "database-only",
                                                  "inventory": "inventory-only", "full": "service-stopped"}[mode],
                                  "source_root": "" if source_root is None else str(source_root), "files": inventory},
                }
                if phases:
                    manifest["version"] = _MOVE_FORMAT_VERSION
                    manifest["resources"]["maintenance_moves"] = phases
                encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
                if len(encoded) > MAX_MANIFEST_BYTES:
                    raise BackupError("备份清单超过大小上限")
                with zipfile.ZipFile(staged.path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    archive.writestr(MANIFEST_MEMBER, encoded)
                    archive.write(snapshot, DATABASE_MEMBER)
                    if include_downloads:
                        for record in inventory:
                            path = root.joinpath(*record["path"].split("/"))
                            with archive.open("downloads/" + record["path"], "w", force_zip64=True) as output:
                                size, digest = _hash_file(path, budget, output)
                            if (size, digest) != (record["size"], record["sha256"]):
                                raise BackupError("资源文件与备份清单不一致")
                if include_downloads:
                    if _inventory(root, budget) != inventory:
                        raise BackupError("资源目录在备份过程中发生变化")
                    for move in moves:
                        budget.check()
                        if _move_phase(root, move) != phases[move.album_id]:
                            raise BackupError("目录整理阶段在备份过程中发生变化")
                    if source.execute("PRAGMA data_version").fetchone()[0] != data_version:
                        raise BackupError("数据库在资源备份过程中发生变化")
            # Inspection repeats CRC/hash/manifest/SQLite validation before replace.
            staged.publish(lambda path: bool(_inspect_backup(path, budget)))
        return BackupInfo(destination, manifest)
    except BackupError:
        raise
    except BackupPathError as error:
        budget.check()
        raise BackupError(f"{error}，未发布新备份；请核对 JMCONSOLE_DOWNLOAD_DIR 是否指向原下载根目录，"
                          "或先迁移资源并校正记录路径和文件名后重试") from None
    except (OSError, sqlite3.Error, zipfile.BadZipFile, zlib.error, RuntimeError, ValueError) as exc:
        if time.monotonic() >= budget.deadline:
            raise BackupTimeout("备份或校验超过时间预算，未发布新备份") from None
        raise BackupError(f"备份失败（{type(exc).__name__}），未发布新备份") from None


def _record(record, *, database=False):
    if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
        raise BackupError("备份文件清单结构无效")
    name = _relative_name(record["path"])
    limit = MAX_DATABASE_BYTES if database else MAX_RESOURCE_BYTES
    if (type(record["size"]) is not int or record["size"] < (1 if database else 0)
            or record["size"] > limit or not isinstance(record["sha256"], str)
            or not _HASH.fullmatch(record["sha256"])):
        raise BackupError("备份文件大小或校验值无效")
    if database and name != DATABASE_MEMBER:
        raise BackupError("备份数据库条目名称无效")


def _manifest(data):
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise BackupError("备份清单不是有效 JSON") from None
    if not isinstance(result, dict) or set(result) != {"format", "version", "created_at", "schema_version", "database", "resources"}:
        raise BackupError("备份清单结构无效")
    if (result["format"] != FORMAT or type(result["version"]) is not int
            or result["version"] not in (FORMAT_VERSION, _MOVE_FORMAT_VERSION)):
        raise BackupError("不支持的备份格式或版本")
    if type(result["schema_version"]) is not int or result["schema_version"] < 0:
        raise BackupError("备份数据库版本无效")
    try:
        if not isinstance(result["created_at"], str) or datetime.fromisoformat(result["created_at"]).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise BackupError("备份创建时间无效") from None
    _record(result["database"], database=True)
    resources = result["resources"]
    fields = {"mode", "included", "consistency", "source_root", "files"}
    has_moves = result["version"] == _MOVE_FORMAT_VERSION
    if has_moves:
        fields.add("maintenance_moves")
    if not isinstance(resources, dict) or set(resources) != fields:
        raise BackupError("备份资源清单结构无效")
    modes = {"none": (False, "database-only"), "database": (False, "database-only"),
             "inventory": (False, "inventory-only"), "full": (True, "service-stopped")}
    if (not isinstance(resources["mode"], str) or resources["mode"] not in modes
            or type(resources["included"]) is not bool
            or (resources["included"], resources["consistency"]) != modes[resources["mode"]]
            or not isinstance(resources["source_root"], str) or "\x00" in resources["source_root"]
            or not isinstance(resources["files"], list) or len(resources["files"]) > MAX_FILES):
        raise BackupError("备份资源模式无效")
    if resources["mode"] == "none":
        if resources["source_root"] or resources["files"]:
            raise BackupError("仅数据库备份不能包含资源来源或清单")
    elif not (PurePosixPath(resources["source_root"]).is_absolute()
              or PureWindowsPath(resources["source_root"]).is_absolute()):
        raise BackupError("资源备份缺少原始下载目录")
    if resources["mode"] == "database" and resources["files"]:
        raise BackupError("仅数据库快照不能声称包含资源清单")
    if has_moves:
        phases = resources["maintenance_moves"]
        if (not resources["included"] or result["schema_version"] < 5 or not isinstance(phases, dict)
                or not 0 < len(phases) <= MAX_FILES
                or any(not validate_numeric(album) or not isinstance(phase, str) or phase not in _MOVE_PHASES
                       for album, phase in phases.items())):
            raise BackupError("备份中的目录整理阶段无效")
    names, total = set(), 0
    for record in resources["files"]:
        _record(record)
        name = record["path"].casefold()
        if name in names:
            raise BackupError("备份资源清单存在重复或大小写冲突")
        names.add(name)
        total += record["size"]
    if total > MAX_TOTAL_BYTES:
        raise BackupError("备份资源总大小超过上限")
    return result


def _open_backup_zip(handle, check):
    try:
        return BoundedZipFile(handle, max_entries=MAX_FILES + 2, max_directory_bytes=MAX_DIRECTORY_BYTES, check=check)
    except ZipMetadataLimit as error:
        raise BackupError(str(error)) from None


def _inspect_backup(path_or_stream, budget):
    archive_path = None
    if isinstance(path_or_stream, (str, os.PathLike)):
        archive_path = _absolute(path_or_stream)
        _plain_path(archive_path)
    source = archive_path.open("rb") if archive_path is not None else nullcontext(path_or_stream)
    with source as handle, _open_backup_zip(handle, budget.check) as archive:
        infos = archive.infolist()
        names, exact, total = set(), {}, 0
        for info in infos:
            budget.check()
            name = _relative_name(info.filename)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if (info.orig_filename != info.filename or name.casefold() in names or info.is_dir() or info.flag_bits & 1
                    or kind not in (0, stat.S_IFREG) or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise BackupError("备份归档包含重复、链接、目录或不支持的条目")
            names.add(name.casefold())
            exact[name] = info
            total += info.file_size
            if total > MAX_TOTAL_BYTES + MAX_DATABASE_BYTES + MAX_MANIFEST_BYTES:
                raise BackupError("备份归档解压大小超过上限")
        manifest_info = exact.get(MANIFEST_MEMBER)
        if manifest_info is None or not 0 < manifest_info.file_size <= MAX_MANIFEST_BYTES:
            raise BackupError("备份缺少有效清单")
        manifest = _manifest(archive.read(manifest_info))
        expected = {MANIFEST_MEMBER, DATABASE_MEMBER}
        records = {DATABASE_MEMBER: manifest["database"]}
        if manifest["resources"]["included"]:
            for record in manifest["resources"]["files"]:
                name = "downloads/" + record["path"]
                expected.add(name)
                records[name] = record
        if set(exact) != expected:
            raise BackupError("归档条目与备份清单不一致")
        with tempfile.TemporaryDirectory(prefix="jm-backup-inspect-") as temporary:
            database = Path(temporary) / DATABASE_MEMBER
            for name, record in records.items():
                info = exact[name]
                if info.file_size != record["size"]:
                    raise BackupError("归档文件大小与清单不一致")
                digest, size = hashlib.sha256(), 0
                with archive.open(info) as source:
                    output = database.open("wb") if name == DATABASE_MEMBER else None
                    try:
                        while True:
                            budget.check()
                            chunk = source.read(_CHUNK)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > record["size"]:
                                raise BackupError("归档文件超出清单大小")
                            digest.update(chunk)
                            if output is not None:
                                output.write(chunk)
                    finally:
                        if output is not None:
                            output.close()
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise BackupError("备份文件校验失败")
            if _check_database(database, budget) != manifest["schema_version"]:
                raise BackupError("SQLite 快照版本与清单不一致")
    budget.check()
    return BackupInfo(archive_path, manifest)


def inspect_backup(path_or_stream, *, timeout=120) -> BackupInfo:
    """Fully validate without modifying the archive; caller-owned streams stay open.

    Restore callers may keep one open binary handle through validation and copying,
    preventing a replaced pathname from changing the archive that is restored.
    """
    budget = _Budget(timeout)
    try:
        return _inspect_backup(path_or_stream, budget)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile, zlib.error, RuntimeError, EOFError, ValueError) as exc:
        if time.monotonic() >= budget.deadline:
            raise BackupTimeout("备份或校验超过时间预算") from None
        raise BackupError(f"备份校验失败（{type(exc).__name__}）") from None


validate_backup = inspect_backup
