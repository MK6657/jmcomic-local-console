"""Cold HTML discovery and SDK session ownership, without socket or libcurl I/O."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

import pytest


@pytest.fixture
def cold_html(client, monkeypatch):
    from curl_cffi.requests import Session
    from jmcomic import JmModuleConfig
    from core.settings import update_settings
    update_settings({"client_type": "html", "proxy": "http://proxy.invalid:9876", "timeout": 7})
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_HTML_LIST", None)
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_HTML", None, raising=False)
    monkeypatch.setattr(JmModuleConfig, "DEFAULT_PROXIES", {"all": "http://unwanted.invalid"})
    sessions, closed = [], []
    closed_event = threading.Event()
    original_init, original_close = Session.__init__, Session.close

    def init(session, *args, **kwargs):
        original_init(session, *args, **kwargs)
        sessions.append(session)

    def close(session):
        closed.append(session)
        original_close(session)
        closed_event.set()

    monkeypatch.setattr(Session, "__init__", init)
    monkeypatch.setattr(Session, "close", close)
    return SimpleNamespace(sessions=sessions, closed=closed, closed_event=closed_event)


@pytest.mark.parametrize("source", ["web", "cli", "check"])
def test_cold_html_uses_saved_proxy_and_closes_probe(cold_html, monkeypatch, source):
    from curl_cffi.requests import Session
    from core import jm_service
    import jmcomic_cli
    seen = []

    def get(session, url, **kwargs):
        seen.append((session, kwargs))
        return SimpleNamespace(status_code=302, headers={"Location": "https://discovered.invalid/"})

    monkeypatch.setattr(Session, "get", get)
    if source == "check":
        instance = jm_service.new_check_client(2)
    elif source == "cli":
        instance, option = jmcomic_cli.get_client()
        assert option.build_jm_client() is instance
    else:
        instance, _ = jm_service.get_client(shared=False)
    try:
        assert instance.get_domain_list() == ["discovered.invalid"]
        [(probe, kwargs)] = seen
        assert kwargs["allow_redirects"] is False
        assert 0 < kwargs.get("timeout", probe.timeout) <= (2 if source == "check" else 7)
        for session in (probe, instance.get_root_postman().session):
            assert session.proxies == dict.fromkeys(("http", "https"), "http://proxy.invalid:9876")
        assert cold_html.closed == [probe]
    finally:
        jm_service.close_client(instance)
    assert len(cold_html.sessions) == len(cold_html.closed) == 2


def test_image_client_never_needs_a_cold_html_domain(cold_html):
    from core import jm_service
    instance = jm_service.new_image_client()
    try:
        assert instance.client_key == "html"
        assert instance.get_root_postman().session._use_thread_local_curl is False
    finally:
        jm_service.close_client(instance)
    assert cold_html.sessions == cold_html.closed


@pytest.mark.parametrize("source", ["web", "cli"])
@pytest.mark.parametrize("failure", ["discovery", "constructor"])
def test_sdk_failures_close_every_allocated_session(cold_html, monkeypatch, source, failure):
    from curl_cffi.requests import Session
    from jmcomic import JmHtmlClient
    from core import jm_service
    import jmcomic_cli

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(Session, "get", fail if failure == "discovery" else lambda *args, **kwargs:
                        SimpleNamespace(status_code=302, headers={"Location": "https://discovered.invalid/"}))
    if failure == "constructor":
        monkeypatch.setattr(JmHtmlClient, "after_init", fail)
    before = jm_service.get_active_client_count()
    with pytest.raises(OSError, match="injected"):
        jmcomic_cli.get_client() if source == "cli" else jm_service.get_client(shared=False)
    assert cold_html.sessions and sorted(map(id, cold_html.closed)) == sorted(map(id, cold_html.sessions))
    assert jm_service.get_active_client_count() == before


def test_redirects_share_a_deadline_and_fail_without_caching(cold_html, monkeypatch):
    from curl_cffi.requests import Session
    from core import jm_service
    attempts = []

    def redirect(session, url, **kwargs):
        attempts.append(kwargs.get("timeout", session.timeout))
        if len(attempts) >= 10:
            pytest.fail("unbounded redirect loop")
        time.sleep(0.04)
        return SimpleNamespace(status_code=301, headers={"Location": f"https://hop{len(attempts)}.invalid"})

    monkeypatch.setattr(Session, "get", redirect)
    with pytest.raises(TimeoutError):
        jm_service.new_check_client(0.1)
    assert cold_html.closed_event.wait(1)
    assert 1 <= len(attempts) <= 3 and 0 < attempts[-1] <= attempts[0] <= 0.1
    assert cold_html.sessions == cold_html.closed
    monkeypatch.setattr(Session, "get", lambda *args, **kwargs:
                        SimpleNamespace(status_code=302, headers={"Location": "https://recovered.invalid/"}))
    recovered = jm_service.new_check_client(1)
    try:
        assert recovered.get_domain_list() == ["recovered.invalid"]
    finally:
        jm_service.close_client(recovered)


@pytest.mark.parametrize("kind", ["shared", "independent", "image", "check"])
def test_slow_factory_cannot_block_invalidation_or_return_a_stale_client(client, monkeypatch, kind):
    from core import jm_service, sdk_adapter
    from core.settings import update_settings
    entered, release = threading.Event(), threading.Event()
    built = []
    original = sdk_adapter.new_client

    def slow(*args, **kwargs):
        candidate = original(*args, **kwargs)
        built.append(candidate)
        if len(built) == 1:
            entered.set()
            assert release.wait(3)
        return candidate

    monkeypatch.setattr(sdk_adapter, "new_client", slow)
    factory = {"shared": lambda: jm_service.get_client()[0], "independent": lambda: jm_service.get_client(False)[0],
               "image": jm_service.new_image_client, "check": lambda: jm_service.new_check_client(1)}[kind]
    before = jm_service.get_active_client_count()
    with ThreadPoolExecutor(2) as pool:
        building = pool.submit(factory)
        try:
            assert entered.wait(1)
            update_settings({"client_type": "html"})
            assert pool.submit(jm_service.invalidate_option_cache).result(1)
        finally:
            release.set()
        current = building.result(2)
    try:
        assert current is not built[0] and current.client_key == "html"
        assert built[0].get_root_postman().session._closed
        assert jm_service.get_active_client_count() == before + (kind in ("independent", "check"))
    finally:
        jm_service.close_client(current)
    assert jm_service.get_active_client_count() == before


def test_late_probe_cannot_replace_new_generation_or_diagnostic_domain(cold_html, monkeypatch):
    from curl_cffi.requests import Session
    from jmcomic import JmModuleConfig
    from core import jm_service
    entered, release = threading.Event(), threading.Event()
    probes = []

    def get(session, url, **kwargs):
        probes.append(session)
        stale = len(probes) == 1
        if stale:
            entered.set()
            assert release.wait(3)
        host = "old.invalid" if stale else "new.invalid"
        return SimpleNamespace(status_code=302, headers={"Location": f"https://{host}"})

    monkeypatch.setattr(Session, "get", get)
    with ThreadPoolExecutor(2) as pool:
        waiting = pool.submit(jm_service.get_client)
        try:
            assert entered.wait(1)
            assert pool.submit(jm_service.invalidate_option_cache).result(1)
            current = pool.submit(jm_service.new_check_client, 2).result(1)
            jm_service.close_client(current)
            assert current.get_domain_list() == ["new.invalid"]
        finally:
            release.set()
        shared, _ = waiting.result(2)
    try:
        assert shared.get_domain_list() == ["new.invalid"]
        assert len(probes) == 2 and all(probe._closed for probe in probes)
        assert JmModuleConfig.DOMAIN_HTML == "new.invalid"
    finally:
        jm_service.close_client(shared)
    jm_service.invalidate_option_cache()
    assert not JmModuleConfig.DOMAIN_HTML


def test_blocked_probe_is_shared_but_waiters_have_separate_deadlines(cold_html, monkeypatch):
    from curl_cffi.requests import Session
    from core import jm_service
    entered, release = threading.Event(), threading.Event()
    probes = []

    def get(session, url, **kwargs):
        probes.append(session)
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(status_code=302, headers={"Location": "https://shared.invalid"})

    monkeypatch.setattr(Session, "get", get)
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(jm_service.get_client)
        try:
            assert entered.wait(1)
            waiter = pool.submit(jm_service.new_check_client, 0.1)
            with pytest.raises(TimeoutError):
                waiter.result(1)
            assert waiter.done() and not owner.done() and not cold_html.closed
        finally:
            release.set()
        shared, _ = owner.result(1)
    try:
        independent, _ = jm_service.get_client(False)
        jm_service.close_client(independent)
        assert len(probes) == 1 and probes[0]._closed
    finally:
        jm_service.close_client(shared)


def test_blank_proxy_is_direct_even_with_sdk_and_environment_proxies(cold_html, monkeypatch):
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session
    from core import jm_service
    from core.settings import update_settings
    update_settings({"proxy": ""})
    monkeypatch.setenv("HTTPS_PROXY", "http://env.invalid:9876")
    monkeypatch.setattr(Session, "get", lambda *args, **kwargs:
                        SimpleNamespace(status_code=302, headers={"Location": "https://direct.invalid/"}))
    instance, _ = jm_service.get_client(False)
    try:
        assert len(cold_html.sessions) == 2
        for session in cold_html.sessions:
            assert not session.proxies and session.curl_options[CurlOpt.NOPROXY] == "*"
    finally:
        jm_service.close_client(instance)


def test_option_never_writes_saved_proxy_into_sdk_process_defaults(cold_html):
    from jmcomic import JmModuleConfig
    from core import sdk_adapter
    before = dict(JmModuleConfig.DEFAULT_PROXIES)
    option = sdk_adapter.build_option()
    assert JmModuleConfig.DEFAULT_PROXIES == before
    assert option.client.postman.meta_data.get("proxies") == dict.fromkeys(
        ("http", "https"), "http://proxy.invalid:9876")


def test_shutdown_owns_an_unpublished_shared_session_until_factory_exits(client, monkeypatch):
    from core import jm_service, sdk_adapter
    from core.background_calls import BackgroundCalls, BackgroundCallsStopped
    calls = BackgroundCalls()
    calls.set_shutdown_cleanup(jm_service._shutdown_client_cache)
    monkeypatch.setattr(jm_service, "background_calls", calls)
    entered, release = threading.Event(), threading.Event()
    sessions = []
    original = sdk_adapter.new_client

    def slow(*args, **kwargs):
        instance = original(*args, **kwargs)
        sessions.append(instance.get_root_postman().session)
        entered.set()
        assert release.wait(3)
        return instance

    monkeypatch.setattr(sdk_adapter, "new_client", slow)
    with ThreadPoolExecutor(1) as pool:
        request = pool.submit(jm_service.get_client)
        try:
            assert entered.wait(1)
            assert not calls.stop(0.05)
            assert not sessions[0]._closed
        finally:
            release.set()
        with pytest.raises(BackgroundCallsStopped):
            request.result(1)
    assert calls.stop(1) and sessions[0]._closed and jm_service._global_client is None
