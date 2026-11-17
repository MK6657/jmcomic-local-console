"""Code resources and process-start instance paths, without business imports.

Environment overrides are read once when this module is imported. Changing a
running process's environment never relocates its files. The explicit parser is
pure: it validates a proposed configuration without creating or moving data.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Mapping


PATH_ENVIRONMENT = ("JMCONSOLE_HOME", "JMCONSOLE_DOWNLOAD_DIR",
                    "JMCONSOLE_CACHE_DIR", "JMCONSOLE_BACKUP_DIR")


def _environment_paths(environment: Mapping[str, str]) -> dict[str, Path]:
    result = {}
    for name in PATH_ENVIRONMENT:
        if name not in environment:
            continue
        value = environment[name]
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"{name} 必须是非空的绝对路径")
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"{name} 必须是非空的绝对路径")
        result[name] = Path(os.path.normpath(path))
    return result


# Capture only path settings, not proxy credentials or the rest of the process environment.
_PATH_OVERRIDES = _environment_paths(os.environ)


@dataclass(frozen=True)
class PathConfig:
    home: Path
    data: Path
    logs: Path
    downloads: Path
    cache: Path
    backups: Path

    def as_environment(self) -> dict[str, str]:
        """Pin a child process to this same instance and storage configuration."""
        return dict(zip(PATH_ENVIRONMENT, map(str, (self.home, self.downloads, self.cache, self.backups))))

    def instance_id(self, launch_token: str = "") -> str:
        """Bind this launch to its effective storage roots without publishing paths or its nonce."""
        roots = [os.path.normcase(path) for path in self.as_environment().values()]
        return hashlib.sha256(json.dumps(["jmconsole-instance-v1", launch_token, *roots]).encode("ascii")).hexdigest()


def get_path_config(environment: Mapping[str, str] | None = None,
                    *, default_home: Path | None = None) -> PathConfig:
    """Validate without creating/moving files; omitted environment uses the startup snapshot.

    With no explicit default, derived paths follow get_app_root(), preserving the
    existing temporary-instance injection used by offline tests and smoke checks.
    """
    overrides = _PATH_OVERRIDES if environment is None else _environment_paths(environment)
    default = Path(default_home) if default_home is not None else (
        get_app_root() if environment is None else _default_app_root())
    if not default.is_absolute():
        raise ValueError("default_home 必须是绝对路径")
    home = overrides.get("JMCONSOLE_HOME", default)
    return PathConfig(home, home / "runtime" / "data", home / "runtime" / "logs",
                      overrides.get("JMCONSOLE_DOWNLOAD_DIR", home / "downloads"),
                      overrides.get("JMCONSOLE_CACHE_DIR", home / "runtime" / "cache"),
                      overrides.get("JMCONSOLE_BACKUP_DIR", home / "runtime" / "backups"))


def get_resource_root() -> Path:
    """获取只读资源根目录（Flask templates/static 所在目录）。

    打包后：sys._MEIPASS（exe 内部的临时解压目录）
    开发时：项目根目录
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def get_app_root() -> Path:
    """Instance home, defaulting to the portable executable or project directory."""
    return _PATH_OVERRIDES.get("JMCONSOLE_HOME") or _default_app_root()


def _default_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_data_dir() -> Path:
    return get_path_config().data


def get_log_dir() -> Path:
    return get_path_config().logs


def get_download_root() -> Path:
    return get_path_config().downloads


def get_cache_dir() -> Path:
    return get_path_config().cache


def get_backup_dir() -> Path:
    return get_path_config().backups


def ensure_dirs() -> None:
    """Create current runtime roots; backups are created only by an explicit backup."""
    paths = get_path_config()
    for path in (paths.data, paths.logs, paths.downloads, paths.cache):
        path.mkdir(parents=True, exist_ok=True)
