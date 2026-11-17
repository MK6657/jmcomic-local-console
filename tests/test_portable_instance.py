import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path, arguments, stdin="", *, script=None, encoding="cp1252:strict"):
    environment = dict(os.environ, JMCONSOLE_HOME=str(tmp_path / "原实例"),
                       JMCONSOLE_DOWNLOAD_DIR=str(tmp_path / "原图片目录"), PYTHONIOENCODING=encoding)
    program = [sys.executable, str(ROOT / "desktop.py")] if script is None else [sys.executable, "-c", script]
    return subprocess.run([*program, *arguments], cwd=ROOT, env=environment, input=stdin,
                          capture_output=True, text=True, encoding="utf-8", timeout=15)


def test_view_current_or_selected_paths_with_redirected_non_chinese_console(tmp_path):
    other = tmp_path / "旧版本 目录"
    for args, expected in ((["instance"], tmp_path / "原实例"), (["instance", "--home", str(other)], other)):
        result = _run(tmp_path, args)
        assert result.returncode == 0, result.stderr
        assert str(expected / "runtime/data/app.db") in result.stdout
        assert str(tmp_path / "原图片目录") in result.stdout
        assert "JMComic.exe attach" in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("answer", ["\n", "n\n", "", "anything\n"])
def test_cancel_selection_before_opening_any_instance(tmp_path, answer):
    other = tmp_path / "旧版本 目录"
    database = other / "runtime/data/app.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"existing database must not be initialized or upgraded")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = _run(tmp_path, ["launch", "--home", str(other)], answer)
    assert result.returncode == 0, result.stderr
    assert "已取消" in result.stdout
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert not (tmp_path / "原实例").exists() and not (tmp_path / "原图片目录").exists()
    assert not (other / "runtime/logs").exists()


def test_confirmed_missing_or_relative_home_never_creates_a_new_instance(tmp_path):
    for home in (str(tmp_path / "typo"), "relative"):
        result = _run(tmp_path, ["launch", "--home", home], "y\n")
        assert result.returncode == 1 and result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command,arguments", [("attach", ["--attach"]), ("launch", ["--wait"])])
def test_explicit_home_reaches_startup_bindings_and_preserves_storage_overrides(tmp_path, command, arguments):
    other = tmp_path / "旧版本 目录"
    database = other / "runtime/data/app.db"
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version=1")
    script = """
import json, os, sys, types, desktop
def launch(args):
    from core.database import DB_PATH
    from core.path_utils import get_path_config
    print(json.dumps([args, str(DB_PATH), str(get_path_config().downloads)]))
    return 0
sys.modules['launcher'] = types.SimpleNamespace(main=launch)
raise SystemExit(desktop.main())
"""
    result = _run(tmp_path, [command, "--home", str(other)], "y\n", script=script, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.dumps([arguments, str(database), str(tmp_path / "原图片目录")]) in result.stdout
    assert not (tmp_path / "原实例").exists()
    assert not (other / "runtime/logs").exists()


@pytest.mark.parametrize("command", ["serve", "self-check", "lifecycle-check"])
def test_private_or_isolated_commands_reject_home_before_dispatch(tmp_path, command):
    result = _run(tmp_path, [command, "--home", str(tmp_path / "unexpected")])
    assert result.returncode == 2 and "--home" in result.stderr
    assert list(tmp_path.iterdir()) == []
