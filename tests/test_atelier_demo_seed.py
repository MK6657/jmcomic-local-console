import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from tools import seed_atelier_demo as demo


def run_seed(home, *, keep=True, success=True):
    outside = home.parent / 'unrelated-instance'
    environment = dict(os.environ, PYTHONIOENCODING='utf-8', JMCONSOLE_HOME=str(outside),
                       JMCONSOLE_DOWNLOAD_DIR=str(outside / 'books'), JMCONSOLE_CACHE_DIR=str(outside / 'cache'),
                       JMCONSOLE_BACKUP_DIR=str(outside / 'backups'))
    runner = """
import json, socket, sys
from pathlib import Path
from curl_cffi.curl import Curl
def offline(*args, **kwargs):
    raise AssertionError('demo seeding must stay offline')
socket.socket.connect = socket.create_connection = Curl.perform = offline
from tools.seed_atelier_demo import seed_demo
print(json.dumps(seed_demo(Path(sys.argv[1]), keep_existing=sys.argv[2] == 'keep')))
"""
    result = subprocess.run([sys.executable, '-B', '-c', runner, str(home), 'keep' if keep else 'build'],
                            cwd=demo.ATELIER_ROOT, env=environment, encoding='utf-8', capture_output=True, timeout=90)
    assert not outside.exists()
    if success:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.splitlines()[-1])
    assert result.returncode != 0, result.stdout
    return result.stderr


def test_demo_creates_keeps_and_rebuilds_its_own_offline_samples(tmp_path):
    home = tmp_path / 'dedicated'
    summary = run_seed(home)
    assert not summary['reused'] and summary['books_seeded'] == 6
    database = Path(summary['db_path'])
    assert database == home / 'runtime/data/app.db'
    connection = sqlite3.connect(database)
    try:
        assert connection.execute('SELECT count(*) FROM album_detail_cache').fetchone() == (6,)
        assert connection.execute("SELECT value FROM settings WHERE key='auto_update_check'").fetchone() == ('false',)
        assert {row[0] for row in connection.execute('SELECT DISTINCT status FROM jobs')} >= {
            'paused', 'completed', 'failed', 'canceled'}
        connection.execute("UPDATE settings SET value='night' WHERE key='ui_theme'")
        connection.execute("UPDATE settings SET value='true' WHERE key='auto_update_check'")
        connection.commit()
    finally:
        connection.close()
    assert len(list((home / 'downloads').rglob('*.cbz'))) == 1
    assert run_seed(home)['reused']
    connection = sqlite3.connect(database)
    try:
        assert dict(connection.execute("SELECT key,value FROM settings WHERE key IN ('ui_theme','auto_update_check')")) == {
            'ui_theme': 'night', 'auto_update_check': 'false'}
    finally:
        connection.close()
    personal = home / 'note.txt'
    personal.write_text('keep')
    assert run_seed(home, keep=False)['books_seeded'] == 6
    assert personal.read_text() == 'keep'


@pytest.mark.parametrize('keep', [True, False])
def test_unmarked_instance_never_opened_or_removed(tmp_path, keep):
    home = tmp_path / '_demo_home'
    data = home / 'runtime/data'
    data.mkdir(parents=True)
    database = data / 'app.db'
    database.write_bytes(b'unmanaged database')
    assert '不是本脚本创建' in run_seed(home, keep=keep, success=False)
    assert database.read_bytes() == b'unmanaged database'
    assert not (home / demo.OWNER_FILE).exists()


def test_copied_marker_and_existing_empty_directory_not_adopted(tmp_path, monkeypatch):
    home = tmp_path / 'new'
    assert demo._prepare_home(home)
    other = tmp_path / 'existing'
    other.mkdir()
    with pytest.raises(ValueError, match='不是本脚本创建'):
        demo._prepare_home(other)
    shutil.copyfile(home / demo.OWNER_FILE, other / demo.OWNER_FILE)
    with pytest.raises(ValueError, match='归属标识不匹配'):
        demo._prepare_home(other)
    monkeypatch.setenv('JMCONSOLE_HOME', str(other))
    monkeypatch.setattr(demo, 'DEFAULT_DEMO_HOME', tmp_path / 'fresh-default')
    assert demo._resolve_demo_home('') == tmp_path / 'fresh-default'


def test_in_process_cannot_retarget_an_already_bound_application(tmp_path, monkeypatch):
    home = tmp_path / 'unbound'
    override = str(tmp_path / 'other')
    monkeypatch.setenv('JMCONSOLE_CACHE_DIR', override)
    with pytest.raises(RuntimeError, match='已绑定其他实例'):
        demo.seed_demo(home)
    assert not home.exists() and os.environ['JMCONSOLE_CACHE_DIR'] == override


def make_directory_link(link, target):
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_directory_links_in_ancestors_and_owned_tree_rejected_before_cleanup(tmp_path):
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    sentinel = outside / 'keep.txt'
    sentinel.write_text('keep')
    link = tmp_path / 'alias'
    make_directory_link(link, outside)
    for path in (link, link / 'new'):
        with pytest.raises(ValueError, match='链接或重解析点'):
            demo._resolve_demo_home(path)
    home = tmp_path / 'owned'
    assert demo._prepare_home(home)
    (home / 'runtime').mkdir()
    make_directory_link(home / 'runtime/cache', outside)
    for keep in (False, True):
        assert '链接或重解析点' in run_seed(home, keep=keep, success=False)
    assert sentinel.read_text() == 'keep' and not (outside / 'new').exists()


def test_database_hardlink_is_rejected_before_open(tmp_path):
    home = tmp_path / 'owned'
    assert demo._prepare_home(home)
    data = home / 'runtime/data'
    data.mkdir(parents=True)
    external = tmp_path / 'external.db'
    external.write_bytes(b'external')
    (data / 'app.db').hardlink_to(external)
    assert '硬链接' in run_seed(home, success=False)
    assert external.read_bytes() == b'external'


@pytest.mark.skipif(os.name != 'nt', reason='Windows preview mutex')
def test_running_demo_prevents_a_second_process_from_rebuilding(tmp_path):
    from core.instance_lock import maintenance_lock
    home = tmp_path / 'busy'
    with maintenance_lock(demo.demo_lock_name(home)):
        assert 'InstanceBusyError' in run_seed(home, keep=False, success=False)
    assert not home.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows batch entry')
def test_preview_batch_delegates_without_inherited_instance_overrides(tmp_path):
    preview = tmp_path / 'preview !'
    preview.mkdir()
    shutil.copyfile(demo.ATELIER_ROOT / 'start-demo-preview.bat', preview / 'start-demo-preview.bat')
    (preview / 'desktop.py').write_text(
        "import json, os, sys\nprint(json.dumps([sys.argv[1:], [k for k in os.environ if k.startswith('JMCONSOLE_')]]))\n"
        "sys.exit(17)\n", encoding='utf-8')
    environment = dict(os.environ, JMCONSOLE_DOWNLOAD_DIR='C:\\other dir', JMCONSOLE_HOME=str(tmp_path),
                       JMCONSOLE_BACKUP_DIR='invalid', JMCONSOLE_UI='classic', JMCONSOLE_NEW_OVERRIDE='untrusted & ignored')
    environment['PATH'] = str(Path(sys.executable).parent) + os.pathsep + environment['PATH']
    result = subprocess.run(['cmd.exe', '/d', '/c', 'start-demo-preview.bat'], cwd=preview, env=environment,
                            capture_output=True, encoding='utf-8', timeout=15)
    assert result.returncode == 17, result.stderr
    assert json.loads(result.stdout.strip()) == [['demo'], []]
