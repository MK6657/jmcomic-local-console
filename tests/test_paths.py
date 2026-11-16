"""Startup paths are explicit, portable by default and isolated from code resources."""
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parent.parent
ENV_NAMES = ("JMCONSOLE_HOME", "JMCONSOLE_DOWNLOAD_DIR", "JMCONSOLE_CACHE_DIR", "JMCONSOLE_BACKUP_DIR")


def _child(code, environment):
    env = dict(os.environ)
    for name in ENV_NAMES:
        env.pop(name, None)
    env.update(environment)
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_default_layout_and_explicit_parser_never_create_directories(tmp_path):
    from core.path_utils import get_path_config
    home = tmp_path / "not-created"
    config = get_path_config({}, default_home=home)
    assert config.home == home
    assert config.data == home / "runtime" / "data"
    assert config.logs == home / "runtime" / "logs"
    assert config.downloads == home / "downloads"
    assert config.cache == home / "runtime" / "cache"
    assert config.backups == home / "runtime" / "backups"
    assert not home.exists()
    with pytest.raises(FrozenInstanceError):
        config.home = tmp_path


def test_independent_absolute_overrides_round_trip_to_child_environment(tmp_path):
    from core.path_utils import get_path_config
    env = dict(zip(ENV_NAMES, map(str, (tmp_path / "实例 根", tmp_path / "library",
                                       tmp_path / "cache", tmp_path / "backups"))))
    config = get_path_config(env, default_home=ROOT)
    assert config.home == tmp_path / "实例 根"
    assert config.data == config.home / "runtime" / "data"
    assert config.logs == config.home / "runtime" / "logs"
    assert (config.downloads, config.cache, config.backups) == (
        tmp_path / "library", tmp_path / "cache", tmp_path / "backups")
    assert get_path_config(config.as_environment(), default_home=tmp_path / "different") == config
    assert not config.home.exists() and not config.downloads.exists()


@pytest.mark.parametrize("name", ENV_NAMES)
@pytest.mark.parametrize("value", ["", " ", "relative/directory", ".", "../elsewhere", "bad\x00path"])
def test_invalid_startup_paths_are_rejected_without_echoing_the_value(tmp_path, name, value):
    from core.path_utils import get_path_config
    with pytest.raises(ValueError, match=name):
        get_path_config({name: value}, default_home=tmp_path)


def test_relative_default_is_rejected():
    from core.path_utils import get_path_config
    with pytest.raises(ValueError, match="default_home"):
        get_path_config({}, default_home=Path("relative"))


def test_unset_environment_keeps_original_portable_root_and_code_resources():
    data = _child("""
import json
from core.path_utils import get_app_root, get_resource_root, get_path_config
config = get_path_config()
print(json.dumps({'home': str(get_app_root()), 'resource': str(get_resource_root()),
                  'downloads': str(config.downloads)}))
""", {})
    assert Path(data["home"]) == ROOT
    assert Path(data["resource"]) == ROOT
    assert Path(data["downloads"]) == ROOT / "downloads"


def test_environment_is_captured_once_and_path_import_is_business_free(tmp_path):
    home = tmp_path / "instance"
    env = {"JMCONSOLE_HOME": str(home), "JMCONSOLE_DOWNLOAD_DIR": str(tmp_path / "library")}
    data = _child("""
import json, os, sys
import core.path_utils as paths
before = paths.get_path_config()
os.environ['JMCONSOLE_HOME'] = 'invalid-relative-path'
os.environ['JMCONSOLE_DOWNLOAD_DIR'] = 'another-relative-path'
after = paths.get_path_config()
print(json.dumps({'same': before == after, 'home': str(after.home), 'downloads': str(after.downloads),
                  'resource': str(paths.get_resource_root()),
                  'business_imported': any(name in sys.modules for name in
                    ('jmcomic', 'core.database', 'core.logger', 'core.settings', 'app'))}))
""", env)
    assert data["same"] and not data["business_imported"]
    assert Path(data["home"]) == home
    assert Path(data["downloads"]) == tmp_path / "library"
    assert Path(data["resource"]) == ROOT
    assert not home.exists()


def test_ensure_dirs_uses_injected_instance_and_only_creates_runtime_roots(tmp_path, monkeypatch):
    from core import path_utils
    home = tmp_path / "test-instance"
    monkeypatch.setattr(path_utils, "get_app_root", lambda: home)
    path_utils.ensure_dirs()
    assert all(path.is_dir() for path in (path_utils.get_data_dir(), path_utils.get_log_dir(),
                                          path_utils.get_download_root(), path_utils.get_cache_dir()))
    assert not path_utils.get_backup_dir().exists()


def test_frozen_resources_and_portable_instance_defaults_stay_separate(tmp_path, monkeypatch):
    from core import path_utils
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "extracted"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "portable" / "console.exe"))
    # The test suite replaces get_app_root for isolation; explicit parsing with an
    # empty environment exercises the real executable default without undoing it.
    config = path_utils.get_path_config({})
    assert config.home == tmp_path / "portable"
    assert path_utils.get_resource_root() == tmp_path / "extracted"


def test_old_database_download_root_never_overrides_configured_storage(client, tmp_path, monkeypatch):
    from core import database as db, settings
    actual = tmp_path / "configured-library"
    monkeypatch.setattr(settings, "DOWNLOAD_ROOT", actual)
    db.set_setting("download_root", str(tmp_path / "old-machine"))
    settings.invalidate_settings_cache()
    assert settings.get_settings()["download_root"] == str(actual)
    assert settings.get_setting("download_root") == str(actual)
    assert settings.get_settings()["download_root"] == str(actual)  # cached result remains authoritative
    settings.update_settings({"download_root": str(tmp_path / "attempted-change")})
    assert settings.get_settings()["download_root"] == str(actual)
    assert settings.build_jmcomic_option()["dir_rule"]["base_dir"] == str(actual)


def test_launcher_uses_code_for_execution_and_instance_for_logs(tmp_path, monkeypatch):
    import launcher
    from core.path_utils import get_path_config
    code, home = tmp_path / "source", tmp_path / "instance"
    code.mkdir()
    config = get_path_config({"JMCONSOLE_HOME": str(home)}, default_home=code)
    monkeypatch.setattr(launcher, "get_path_config", lambda **kwargs: config)
    popen = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    launcher.launch_flask(code)
    args, kwargs = popen.call_args
    assert args[0][1] == str(code / "app.py")
    assert kwargs["cwd"] == str(code)
    assert kwargs["env"]["JMCONSOLE_HOME"] == str(home)
    assert kwargs["env"]["JMCONSOLE_DOWNLOAD_DIR"] == str(home / "downloads")
    assert (home / "runtime/logs/launcher.log").is_file()
    assert not (code / "runtime").exists()


def test_launcher_marker_and_child_paths_use_the_same_instance(tmp_path):
    home = tmp_path / "instance"
    data = _child("""
import json
import launcher
from core.path_utils import get_path_config
print(json.dumps({'code_root': str(launcher.ROOT), 'marker': str(launcher.FLASK_JSON),
                  'child': get_path_config().as_environment()}))
""", {"JMCONSOLE_HOME": str(home)})
    assert Path(data["code_root"]) == ROOT
    assert Path(data["marker"]) == home / "runtime/data/flask.json"
    assert Path(data["child"]["JMCONSOLE_HOME"]) == home
    assert not home.exists()


def test_configured_roots_reach_database_guard_logs_and_online_cache_without_network(tmp_path):
    env = dict(zip(ENV_NAMES, map(str, (tmp_path / "instance", tmp_path / "library",
                                      tmp_path / "page-cache", tmp_path / "snapshots"))))
    data = _child("""
import json, socket
def blocked(*args, **kwargs):
    raise AssertionError('No network during path integration checks')
socket.socket.connect = blocked
socket.create_connection = blocked
from curl_cffi.curl import Curl
Curl.perform = blocked
from core import database, logger, online_reader, path_guard, settings
from core.path_utils import ensure_dirs, get_backup_dir
ensure_dirs()
print(json.dumps({'database': str(database.DB_DIR), 'logs': str(logger.LOG_DIR),
                  'downloads': str(path_guard.DOWNLOAD_ROOT), 'online': str(online_reader.CACHE_ROOT),
                  'settings_downloads': settings.DEFAULT_SETTINGS['download_root'],
                  'inside': path_guard.is_safe_path(path_guard.DOWNLOAD_ROOT / 'album' / 'page.webp'),
                  'outside': path_guard.is_safe_path(path_guard.DOWNLOAD_ROOT.parent / 'outside.webp'),
                  'backups_created': get_backup_dir().exists()}))
""", env)
    assert Path(data["database"]) == tmp_path / "instance/runtime/data"
    assert Path(data["logs"]) == tmp_path / "instance/runtime/logs"
    assert Path(data["downloads"]) == tmp_path / "library"
    assert Path(data["settings_downloads"]) == tmp_path / "library"
    assert Path(data["online"]) == tmp_path / "page-cache/online"
    assert data["inside"] and not data["outside"]
    assert not data["backups_created"]
    assert (tmp_path / "instance/runtime/logs/app.log").is_file()
    assert not (tmp_path / "instance/downloads").exists()
