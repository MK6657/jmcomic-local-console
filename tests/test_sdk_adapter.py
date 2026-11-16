"""Shared SDK/CLI construction contracts. All transports are blocked offline."""
from copy import deepcopy
import logging
from pathlib import Path
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
    def construct(received):
        received["client"]["domain"]["api"].append("mutated.invalid")
        return "constructed"
    monkeypatch.setattr(JmModuleConfig, "option_class", lambda: SimpleNamespace(construct=construct))
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


def test_cli_initializes_a_fresh_database_before_reading_settings(tmp_path, monkeypatch):
    import jmcomic_cli as cli
    from core import database as db, settings
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fresh.db")
    settings.invalidate_settings_cache()
    assert not db.DB_PATH.exists()
    instance, _ = cli.get_client()
    try:
        assert db.DB_PATH.exists()
        assert instance.client_key == settings.DEFAULT_SETTINGS["client_type"]
        conn = db.get_db()
        try:
            assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        _close(instance)
        settings.invalidate_settings_cache()


@pytest.mark.parametrize("command", ["search", "info", "browse", "download", "batch"])
def test_cli_commands_share_one_explicit_option_and_close_the_client(client, monkeypatch, capsys, command):
    import jmcomic
    import jmcomic_cli as cli
    instance, option = Mock(), SimpleNamespace(dir_rule=SimpleNamespace())
    monkeypatch.setattr(cli, "get_client", lambda: (instance, option))
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
    batch = Mock(return_value=[])
    monkeypatch.setattr(jmcomic, "download_album", download)
    monkeypatch.setattr(jmcomic, "download_batch", batch)
    args = SimpleNamespace(keyword="fixture", page=1, album_id="123", show_chapters=False,
                           category="all", order="latest", time="all", ids="123,456,123")
    getattr(cli, f"cmd_{command}")(args)
    close.assert_called_once_with(instance)
    if command == "download":
        download.assert_called_once_with("123", option=option)
    elif command == "batch":
        batch.assert_called_once_with(download, ["123", "456"], option=option)
    else:
        download.assert_not_called()
        batch.assert_not_called()
