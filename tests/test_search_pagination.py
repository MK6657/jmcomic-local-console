"""Search UI pages over a local, ordered SDK result set. No upstream requests."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jmcomic import JmModuleConfig, JmSearchPage

from core import jm_service, metadata_budget
from core.background_calls import BackgroundCalls


@pytest.fixture
def sdk_search(client, monkeypatch):
    instance, _ = jm_service.get_client()
    jm_service.close_client(instance)
    search = Mock()
    monkeypatch.setattr(instance, "search", search)
    return search


def native_results(count, total):
    def fetch(*, page, **kwargs):
        size = JmModuleConfig.PAGE_SIZE_SEARCH
        start = (page - 1) * size
        content = [(str(number), {"name": f"Album {number}"})
                   for number in range(start + 1, min(start + size, count) + 1)]
        return JmSearchPage(content, total, page)
    return fetch


def requested_pages(search):
    return [call.kwargs["page"] for call in search.call_args_list]


def ui_page(client, page, size, keyword="fixture"):
    response = client.get("/api/search", query_string={"q": keyword, "page": page, "page_size": size})
    assert response.status_code == 200, response.json
    assert (response.json["page"], response.json["page_size"]) == (page, size)
    return response.json


@pytest.mark.parametrize("size,expected_calls", [
    (20, [[1], [1], [1], [1], [2], [2]]),
    (50, [[1], [1, 2], [2]]),
    (100, [[1, 2], [2]]),
])
def test_all_ui_pages_and_one_beyond(client, sdk_search, size, expected_calls):
    sdk_search.side_effect = native_results(96, 96)
    seen, next_pages = [], []
    for page, calls in enumerate(expected_calls, 1):
        sdk_search.reset_mock()
        result = ui_page(client, page, size)
        numbers = [int(item["album_id"]) for item in result["items"]]
        assert numbers == list(range((page - 1) * size + 1, min(page * size, 96) + 1))
        assert requested_pages(sdk_search) == calls
        assert result["total"] == 96
        seen.extend(numbers)
        next_pages.append(result.get("has_more"))
    assert seen == list(range(1, 97))
    assert next_pages == [True] * (len(expected_calls) - 2) + [False, False]


@pytest.mark.parametrize("total", [None, 0])
@pytest.mark.parametrize("size", [20, 50, 100])
def test_unknown_total_can_be_paged_to_the_end(client, sdk_search, total, size):
    sdk_search.side_effect = native_results(96, total)
    seen = []
    for page in range(1, 7):
        result = ui_page(client, page, size)
        assert result["total"] is None
        seen.extend(int(item["album_id"]) for item in result["items"])
        if not result["has_more"]:
            break
    assert seen == list(range(1, 97))
    assert page == (96 + size - 1) // size
    beyond = ui_page(client, page + 1, size)
    assert beyond["items"] == [] and beyond["has_more"] is False


@pytest.mark.parametrize("count,total,more,calls", [
    (80, 80, False, [1]), (81, 81, True, [1]),
    (80, None, False, [1, 2]), (81, None, True, [1, 2]),
    (79, 0, False, [1]),
])
def test_next_page_at_a_native_boundary(client, sdk_search, count, total, more, calls):
    sdk_search.side_effect = native_results(count, total)
    result = ui_page(client, 4, 20)
    assert [int(item["album_id"]) for item in result["items"]] == list(range(61, min(count, 80) + 1))
    assert result["has_more"] is more
    assert requested_pages(sdk_search) == calls


def test_native_size_follows_sdk_and_skips_unneeded_pages(client, sdk_search, monkeypatch):
    monkeypatch.setattr(JmModuleConfig, "PAGE_SIZE_SEARCH", 32)
    sdk_search.side_effect = native_results(96, 96)
    result = ui_page(client, 2, 50)
    assert [int(item["album_id"]) for item in result["items"]] == list(range(51, 97))
    assert requested_pages(sdk_search) == [2, 3]


@pytest.mark.parametrize("total", [None, 10001])
def test_clamped_page_has_no_unreachable_next_page(client, sdk_search, total):
    sdk_search.side_effect = native_results(10001, total)
    response = client.get("/api/search?q=fixture&page=501&page_size=20")
    assert response.status_code == 200 and response.json["page"] == 500
    assert [int(item["album_id"]) for item in response.json["items"]] == list(range(9981, 10001))
    assert response.json["has_more"] is False
    assert requested_pages(sdk_search) == [125]


@pytest.mark.parametrize("total", [1, None])
@pytest.mark.parametrize("page,expected", [(1, ["123"]), (2, []), (5, [])])
def test_direct_album_only_appears_on_first_ui_page(client, sdk_search, page, expected, total):
    album = SimpleNamespace(album_id="123", name="Direct", tags=[])
    def direct(*, page, **kwargs):
        result = JmSearchPage.wrap_single_album(album, page)
        result.total = total
        return result
    sdk_search.side_effect = direct
    result = ui_page(client, page, 20, keyword="123")
    assert [item["album_id"] for item in result["items"]] == expected
    assert result["total"] == 1 and result["has_more"] is False
    assert sdk_search.call_count == 1


def test_second_native_page_failure_does_not_publish_partial_results(client, sdk_search):
    sdk_search.side_effect = [native_results(96, 96)(page=1), ConnectionError("offline fixture")]
    response = client.get("/api/search?q=fixture&page_size=100")
    assert (response.status_code, response.json.get("error_kind")) == (502, "connection")
    assert "items" not in response.json
    assert requested_pages(sdk_search) == [1, 2]


@pytest.mark.parametrize("elapsed,budgets", [(3, [5, 2]), (5, [5])])
def test_native_pages_spend_one_deadline(client, sdk_search, monkeypatch, elapsed, budgets):
    now, remaining = [0.0], []
    monkeypatch.setattr(metadata_budget, "_clock", lambda: now[0])
    monkeypatch.setattr(jm_service, "_operation_timeout", lambda cap: 5)
    def fetch(**kwargs):
        remaining.append(metadata_budget.check_current())
        now[0] += elapsed
        return native_results(96, 96)(**kwargs)
    sdk_search.side_effect = fetch
    response = client.get("/api/search?q=fixture&page_size=100")
    assert (response.status_code, response.json.get("error_kind")) == (504, "timeout")
    assert "items" not in response.json
    assert remaining == budgets
    assert sdk_search.call_count == len(budgets)


def test_timeout_on_second_page_keeps_retired_client_leased(client, sdk_search, monkeypatch):
    calls = BackgroundCalls(max_pending=1)
    monkeypatch.setattr(jm_service, "background_calls", calls)
    monkeypatch.setattr(jm_service, "_operation_timeout", lambda cap: 0.2)
    instance, _ = jm_service.get_client()
    jm_service.close_client(instance)
    session = instance.get_root_postman().session
    close = Mock(wraps=session.close)
    monkeypatch.setattr(session, "close", close)
    entered, finish = threading.Event(), threading.Event()
    def fetch(*, page, **kwargs):
        if page == 2:
            entered.set()
            assert finish.wait(3)
        return native_results(96, 96)(page=page)
    sdk_search.side_effect = fetch
    try:
        response = client.get("/api/search?q=fixture&page_size=100")
        assert response.status_code == 504 and entered.is_set()
        jm_service.invalidate_option_cache()
        assert instance._jm_users == 1 and calls.runtime_status()["pending"] == 1
        close.assert_not_called()
    finally:
        finish.set()
        assert calls.stop(2)
    close.assert_called_once_with()
    assert instance._jm_users == 0
