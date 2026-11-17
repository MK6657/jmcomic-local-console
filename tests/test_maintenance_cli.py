"""Exercise backup/restore commands with real disposable SQLite and image data."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest
from PIL import Image


@pytest.fixture
def source(client, tmp_path, monkeypatch):
    from core import database as db, path_utils, instance_lock
    folder = tmp_path / "source-downloads"
    album = folder / "Fixture_123"
    album.mkdir(parents=True)
    Image.new("RGB", (16, 16), "blue").save(album / "00001.png")
    db.add_wishlist("123", "Fixture", "Author", "")
    db.add_album_tag("123", "keep-tag", source="user")
    db.insert_job("job_backup", "123", "Fixture", ["456"])
    db.transition_job_status(
        "job_backup", ["queued", "running", "paused", "completed", "failed", "canceled"], "completed",
        output_path=str(album),
    )
    db.set_setting("proxy", "http://private-user:private-password@proxy.invalid:9876")
    monkeypatch.setattr(path_utils, "get_download_root", lambda: folder)
    monkeypatch.setattr(path_utils, "get_backup_dir", lambda: tmp_path / "backups")
    maintenance = instance_lock.maintenance_lock
    name = "Local\\JMComic_CliMaintenanceTest_" + uuid.uuid4().hex
    monkeypatch.setattr(instance_lock, "maintenance_lock", lambda: maintenance(name))
    return folder, album


def test_full_cli_backup_restore_and_new_instance_reads_without_network(source, tmp_path, capsys):
    import jmcomic_cli as cli
    from core import database as db
    folder, album = source
    before = hashlib.sha256((album / "00001.png").read_bytes()).hexdigest()
    backup = tmp_path / "full.zip"
    assert cli.main(["backup", "--output", str(backup), "--include-downloads"]) == 0
    assert cli.main(["backup-info", str(backup)]) == 0
    target = tmp_path / "restored-instance"
    assert cli.main(["restore", str(backup), "--to", str(target)]) == 0
    output = capsys.readouterr().out
    assert "private-user" not in output and "private-password" not in output
    image = target / "downloads" / album.name / "00001.png"
    assert hashlib.sha256(image.read_bytes()).hexdigest() == before
    with sqlite3.connect(target / "runtime/data/app.db") as restored:
        assert restored.execute("SELECT output_path FROM resource_catalog").fetchone()[0] == str(image.parent)
        assert restored.execute("SELECT tag FROM album_tags").fetchone()[0] == "keep-tag"
        assert restored.execute("SELECT title FROM wishlist").fetchone()[0] == "Fixture"
    assert db.get_job("job_backup")["output_path"] == str(album)
    # A separate process sees the restored paths before importing application modules.
    environment = dict(os.environ, JMCONSOLE_HOME=str(target), PYTHONIOENCODING="utf-8")
    for key in ("JMCONSOLE_DOWNLOAD_DIR", "JMCONSOLE_CACHE_DIR", "JMCONSOLE_BACKUP_DIR"):
        environment.pop(key, None)
    script = """
import json, socket, sys
def blocked(*a, **kw):
    raise AssertionError('network forbidden')
socket.socket.connect=blocked
socket.create_connection=blocked
from curl_cffi.curl import Curl
Curl.perform=blocked
from app import create_app
from core import database as db
with create_app().test_client() as client:
    result=client.get('/api/library/123')
    item=result.get_json()['item']
    print(json.dumps({'status':result.status_code,'readable':item['readable'],'title':item['title']}))
"""
    result = subprocess.run([sys.executable, "-c", script], env=environment,
                            cwd=Path(__file__).resolve().parent.parent,
                            text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": 200, "readable": True, "title": "Fixture"}


def test_inventory_restore_is_explicit_and_existing_target_is_untouched(source, tmp_path, capsys):
    import jmcomic_cli as cli
    from core.backup import inspect_backup
    backup = tmp_path / "inventory.zip"
    assert cli.main(["backup", "--output", str(backup)]) == 0
    assert inspect_backup(backup).includes_downloads is False
    target = tmp_path / "inventory-instance"
    assert cli.main(["restore", str(backup), "--to", str(target)]) == 0
    assert not list((target / "downloads").iterdir())
    assert "未包含资源文件" in capsys.readouterr().out
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    assert cli.main(["restore", str(backup), "--to", str(target)]) == 1
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("command", ["backup", "download", "batch"])
def test_maintenance_and_cli_writers_cannot_start_while_another_owner_exists(source, tmp_path, monkeypatch, capsys, command):
    import jmcomic_cli as cli
    from core import instance_lock
    def refuse():
        raise instance_lock.InstanceBusyError("程序或维护操作已在运行，请先关闭后重试")
    monkeypatch.setattr(instance_lock, "maintenance_lock", refuse)
    monkeypatch.setattr(cli, "get_client", lambda: pytest.fail("blocked commands must not initialize SDK clients"))
    target = tmp_path / "must-not-exist.zip"
    arguments = ["backup", "--output", str(target)] if command == "backup" else [command, "123"]
    assert cli.main(arguments) == 1
    assert not target.exists()
    assert "已在运行" in capsys.readouterr().out
