"""Shared SDK/CLI construction contracts. All transports are blocked offline."""
from contextlib import closing, nullcontext
from copy import deepcopy
import logging
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def _close(instance):
    from core.jm_service import close_client
    close_client(instance)


def test_importing_adapter_does_not_initialize_application_state(tmp_path):
    root = Path(__file__).resolve().parent.parent
    script = """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import core.sdk_adapter
assert 'jmcomic' not in sys.modules
assert 'core.logger' not in sys.modules
assert 'core.database' not in sys.modules
assert 'app' not in sys.modules
assert not list(pathlib.Path.cwd().iterdir())
"""
    result = subprocess.run([sys.executable, "-c", script, str(root)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_sdk_policy_can_be_repeated_without_network_or_duplicate_log_handlers(client, monkeypatch):
    from core import sdk_adapter
    from jmcomic import JmModuleConfig
    from curl_cffi.curl import Curl
    performed = Mock(side_effect=AssertionError("configuration must never connect"))
    monkeypatch.setattr(Curl, "perform", performed)
    monkeypatch.setattr(JmModuleConfig, "FLAG_ENABLE_JM_LOG", False)
    monkeypatch.setattr(JmModuleConfig, "FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN", True)
    monkeypatch.setattr(JmModuleConfig, "FLAG_API_CLIENT_REQUIRE_COOKIES", True)
    sdk_adapter.configure_sdk()
    logger = logging.getLogger("jmcomic")
    before = list(logger.handlers)
    sdk_adapter.configure_sdk()
    assert JmModuleConfig.FLAG_ENABLE_JM_LOG is True
    assert JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN is False
    assert JmModuleConfig.FLAG_API_CLIENT_REQUIRE_COOKIES is False
    assert logger.handlers == before and len({id(handler) for handler in before}) == len(before)
    assert not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
                   for handler in logger.handlers)
    performed.assert_not_called()


@pytest.mark.parametrize("impl", ["api", "html"])
@pytest.mark.parametrize("proxy", ["", "http://sdk-user:sdk-pass@127.0.0.1:9876"])
def test_client_construction_uses_shared_configuration_without_extra_requests(client, monkeypatch, impl, proxy):
    from core import sdk_adapter
    from core.settings import update_settings
    from jmcomic import JmModuleConfig
    from curl_cffi.curl import Curl
    update_settings({"client_type": impl, "proxy": proxy, "timeout": 17, "retry_times": 1})
    domains = {"api": ["api-a.invalid", "api-b.invalid"], "html": ["html-a.invalid"]}
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_API_LIST", domains["api"])
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_HTML_LIST", domains["html"])
    performed = Mock(side_effect=AssertionError("client construction must not send HTTP"))
    discovered = Mock(side_effect=AssertionError("known domains must not be rediscovered"))
    monkeypatch.setattr(Curl, "perform", performed)
    monkeypatch.setattr(JmModuleConfig, "get_html_domain", discovered)
    option = sdk_adapter.build_option()
    instance = sdk_adapter.new_client(option)
    try:
        assert instance.client_key == impl
        assert instance.get_domain_list() == domains[impl]
        assert instance.retry_times == 1
        assert instance.get_meta_data("timeout") == 17
        assert option.client.postman.src_dict["type"] == "curl_cffi_session"
        assert option.client.postman.meta_data.src_dict["impersonate"] == "chrome"
        if proxy:
            assert instance.get_root_postman().session.proxies["https"] == proxy
        performed.assert_not_called()
        discovered.assert_not_called()
    finally:
        _close(instance)


@pytest.mark.parametrize("impl", ["api", "html"])
def test_explicit_domain_map_and_client_overrides_remain_independent(client, impl):
    from core import sdk_adapter
    from core.settings import build_jmcomic_option
    config = build_jmcomic_option()
    config["client"].update(impl=impl, domain={"api": ["api-custom.invalid"], "html": ["html-custom.invalid"]})
    before = deepcopy(config)
    option = sdk_adapter.build_option(config)
    instance = sdk_adapter.new_client(option, timeout=7, cache=False, use_thread_local_curl=False)
    try:
        assert instance.get_domain_list() == [f"{impl}-custom.invalid"]
        assert instance.get_meta_data("timeout") == 7
        assert instance.get_cache_dict() is None
        assert instance.get_root_postman().session._use_thread_local_curl is False
        assert option.client.postman.meta_data.src_dict["timeout"] == 30
        assert config == before
    finally:
        _close(instance)


def test_build_option_does_not_allow_sdk_to_mutate_the_callers_config(client, monkeypatch):
    from core import sdk_adapter
    from jmcomic import JmModuleConfig
    config = {"client": {"domain": {"api": ["api-custom.invalid"]}}}
    before = deepcopy(config)
    class Option:
        @staticmethod
        def construct(received):
            received["client"]["domain"]["api"].append("mutated.invalid")
            return "constructed"
    monkeypatch.setattr(JmModuleConfig, "option_class", lambda: Option)
    assert sdk_adapter.build_option(config) == "constructed"
    assert config == before


def test_cli_returns_the_cached_client_used_by_sdk_downloads(client):
    import jmcomic_cli as cli
    instance, option = cli.get_client()
    try:
        assert option.build_jm_client() is instance
    finally:
        _close(instance)


@pytest.mark.parametrize("impl", ["api", "html"])
def test_cli_and_web_use_matching_saved_settings(client, impl):
    import jmcomic_cli as cli
    from core import jm_service
    from core.settings import update_settings
    update_settings({"client_type": impl, "timeout": 19, "retry_times": 0,
                     "proxy": "http://sdk-user:sdk-pass@127.0.0.1:9876"})
    jm_service.invalidate_option_cache()
    web, web_option = jm_service.get_client(shared=False)
    command, command_option = cli.get_client()
    try:
        assert command.client_key == web.client_key == impl
        assert command.get_domain_list() == web.get_domain_list()
        assert command.get_meta_data("timeout") == web.get_meta_data("timeout") == 19
        assert command.retry_times == web.retry_times == 0
        assert command_option.client.postman.src_dict == web_option.client.postman.src_dict
        assert command_option.dir_rule.base_dir == web_option.dir_rule.base_dir
    finally:
        _close(web)
        _close(command)


@pytest.mark.parametrize("existing", ["missing", "empty"])
def test_cli_reads_default_settings_without_creating_or_initializing_database(tmp_path, monkeypatch, existing):
    import jmcomic_cli as cli
    from core import database as db, settings
    folder = tmp_path / "empty-instance"
    monkeypatch.setattr(db, "DB_DIR", folder)
    monkeypatch.setattr(db, "DB_PATH", folder / "fresh.db")
    if existing == "empty":
        folder.mkdir()
        db.DB_PATH.touch()
    monkeypatch.setattr(db, "init_db", Mock(side_effect=AssertionError("must not initialize a database")))
    instance, _ = cli.get_client()
    try:
        assert instance.client_key == settings.DEFAULT_SETTINGS["client_type"]
        if existing == "empty":
            assert list(folder.iterdir()) == [db.DB_PATH] and not db.DB_PATH.stat().st_size
        else:
            assert not folder.exists()
    finally:
        _close(instance)


def test_cli_read_only_snapshot_includes_committed_wal_settings(tmp_path, monkeypatch):
    import jmcomic_cli as cli
    from core import database as db, instance_lock

    database = tmp_path / "settings # 日常.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "init_db", Mock(side_effect=AssertionError("must not migrate or backfill")))
    monkeypatch.setattr(instance_lock, "maintenance_lock", Mock(side_effect=instance_lock.InstanceBusyError("busy")))
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO settings VALUES ('timeout', '47')")
        writer.commit()
        initial = database.read_bytes()
        wal = database.with_name(f"{database.name}-wal")
        initial_wal = wal.read_bytes()
        instance, _ = cli.get_client()
        try:
            assert instance.get_meta_data("timeout") == 47
        finally:
            _close(instance)
        assert database.read_bytes() == initial and wal.read_bytes() == initial_wal
    db.init_db.assert_not_called()
    instance_lock.maintenance_lock.assert_not_called()


def test_cli_read_only_settings_handles_missing_table_and_invalid_values(tmp_path, monkeypatch):
    import jmcomic_cli as cli
    from core import database as db, settings

    monkeypatch.setattr(db, "init_db", Mock(side_effect=AssertionError("must not initialize a database")))

    no_table = tmp_path / "no-settings-table.db"
    with closing(sqlite3.connect(no_table)) as conn:
        conn.execute("CREATE TABLE other (x TEXT)")
        conn.commit()
    monkeypatch.setattr(db, "DB_PATH", no_table)
    instance, _ = cli.get_client()
    try:
        assert instance.get_meta_data("timeout") == int(settings.DEFAULT_SETTINGS["timeout"])
    finally:
        _close(instance)

    dirty = tmp_path / "dirty-settings.db"
    with closing(sqlite3.connect(dirty)) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO settings VALUES (?, ?)",
            [
                ("timeout", ""),
                ("retry_times", "abc"),
                ("image_format", "invalid_fmt"),
                ("pack_format", "rar"),
                ("download_root", str(tmp_path / "hijacked")),
            ],
        )
        conn.commit()
    monkeypatch.setattr(db, "DB_PATH", dirty)
    loaded = cli._read_only_settings()
    assert loaded["image_format"] == "webp"
    assert loaded["pack_format"] == "cbz"
    assert loaded["download_root"] == str(settings.DOWNLOAD_ROOT)
    instance, option = cli.get_client()
    try:
        assert instance.get_meta_data("timeout") == int(settings.DEFAULT_SETTINGS["timeout"])
        assert instance.retry_times == int(settings.DEFAULT_SETTINGS["retry_times"])
        assert option.dir_rule.base_dir == str(settings.DOWNLOAD_ROOT)
    finally:
        _close(instance)


@pytest.mark.parametrize("command", ["search", "info", "browse", "download", "batch"])
def test_cli_commands_share_one_explicit_option_and_close_the_client(client, monkeypatch, capsys, command):
    import jmcomic
    import jmcomic_cli as cli
    instance, option = Mock(), SimpleNamespace(dir_rule=SimpleNamespace())
    get_client = Mock(return_value=(instance, option))
    monkeypatch.setattr(cli, "get_client", get_client)
    close = Mock()
    monkeypatch.setattr(cli, "close_client", close)
    instance.search.return_value = SimpleNamespace(content=[], total=0)
    instance.categories_filter.return_value = SimpleNamespace(content=[], total=0)
    class Album:
        name, author, album_id = "Title", "Author", "123"
        tags, actors, works, views, likes = [], [], [], 0, 0
        def __len__(self):
            return 0
    album = Album()
    instance.get_album_detail.return_value = album
    download = Mock(return_value=(album, SimpleNamespace(all_success=True, download_failed_image=[], option=option)))
    batch = Mock(return_value=jmcomic.BatchResult())
    monkeypatch.setattr(jmcomic, "download_album", download)
    monkeypatch.setattr(jmcomic, "download_batch", batch)
    args = SimpleNamespace(keyword="fixture", page=1, album_id="123", show_chapters=False,
                           category="all", order="latest", time="all", ids="123,456,123")
    getattr(cli, f"cmd_{command}")(args)
    close.assert_called_once_with(instance)
    if command in {"batch", "download"}:
        get_client.assert_called_once_with(read_only=False)
    else:
        get_client.assert_called_once_with()
    if command == "download":
        download.assert_called_once_with("123", option=option)
    elif command == "batch":
        batch.assert_called_once_with(download, ["123", "456"], option=option)
    else:
        download.assert_not_called()
        batch.assert_not_called()


@pytest.mark.parametrize("dispatch", ["source", "portable"])
@pytest.mark.parametrize("succeeds,expected", [(2, 0), (1, 1), (0, 1), (None, 1)])
def test_batch_exit_codes_with_real_sdk_scheduler(monkeypatch, capsys, dispatch, succeeds, expected):
    import desktop
    import jmcomic
    import jmcomic_cli as cli
    from core import instance_lock

    option, instance = object(), object()
    acquire = Mock(return_value=(instance, option))
    close = Mock()
    monkeypatch.setattr(cli, "get_client", acquire)
    monkeypatch.setattr(cli, "close_client", close)
    monkeypatch.setattr(instance_lock, "maintenance_lock", nullcontext)

    def download(album_id, passed, downloader):
        assert passed is option
        if int(album_id) > succeeds:
            raise OSError("synthetic failure")
        return jmcomic.DownloadResult(SimpleNamespace(id=album_id), None)

    batch_item = Mock(side_effect=download)
    monkeypatch.setattr(jmcomic, "download_album", batch_item)
    arguments = ["batch", " , , " if succeeds is None else " JM1,2,1 "]
    result = cli.main(arguments) if dispatch == "source" else desktop.main(["cli", *arguments])
    assert result == expected
    output = capsys.readouterr().out
    if succeeds is None:
        assert "至少需要一个车号" in output
        acquire.assert_not_called()
        batch_item.assert_not_called()
        close.assert_not_called()
    else:
        assert f"{succeeds}/2" in output
        assert ("失败车号" in output) == (succeeds != 2)
        assert {call.args[0] for call in batch_item.call_args_list} == {"1", "2"}
        assert batch_item.call_count == 2
        acquire.assert_called_once_with(read_only=False)
        close.assert_called_once_with(instance)


def test_configure_sdk_wraps_save_image_once_and_preserves_rgba_jpeg_guard(client, tmp_path):
    from PIL import Image
    from core import sdk_adapter
    from jmcomic import JmImageTool

    sdk_adapter.configure_sdk()
    first_func = JmImageTool.save_image.__func__
    assert getattr(JmImageTool.save_image, "_atelier_rgb_guard", False) is True
    for _ in range(1200):
        sdk_adapter.configure_sdk()
    assert JmImageTool.save_image.__func__ is first_func

    jpeg_target = tmp_path / "rgba_page.jpg"
    webp_target = tmp_path / "rgb_page.webp"
    JmImageTool.save_image(Image.new("RGBA", (4, 4), (255, 0, 0, 128)), str(jpeg_target))
    JmImageTool.save_image(Image.new("RGB", (4, 4), (0, 255, 0)), str(webp_target))
    with Image.open(jpeg_target) as saved_jpg, Image.open(webp_target) as saved_webp:
        assert saved_jpg.format == "JPEG" and saved_jpg.mode == "RGB"
        assert saved_webp.format == "WEBP"

