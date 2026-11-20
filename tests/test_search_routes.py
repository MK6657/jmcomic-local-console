from sqlite3 import OperationalError
from unittest.mock import Mock

import pytest

from core import database
from routes import api_search


@pytest.mark.parametrize("keyword", ["A/B", "A B", "中文/外文 +%"])
def test_delete_single_search_history_by_keyword(client, keyword):
    database.add_search_history("other keyword")
    database.add_search_history(keyword)

    response = client.delete("/api/search-history", query_string={"keyword": keyword})

    assert response.status_code == 200 and response.json == {"status": "ok", "deleted": True}
    assert [entry["keyword"] for entry in database.get_search_history()] == ["other keyword"]


def test_delete_search_history_keeps_legacy_route(client):
    database.add_search_history("A B")
    assert client.delete("/api/search-history/A%20B").json["deleted"] is True
    assert client.delete("/api/search-history").status_code == 400


@pytest.mark.parametrize("edition", ["atelier", "classic"])
def test_search_pages_share_the_history_script(client, edition):
    client.set_cookie("jmconsole_edition", edition)
    response = client.get("/search")
    assert response.status_code == 200
    assert b'shared/search-history.js' in response.data


def test_successful_search_records_only_page_one(client, monkeypatch):
    def search(*, page, **_):
        return {"items": [{"title": "fixture"}], "total": 30, "has_more": True, "page": page}

    record = Mock()
    monkeypatch.setattr(api_search, "search_albums", search)
    monkeypatch.setattr(api_search, "add_search_history", record)
    for page in (1, 2, 3):
        response = client.get("/api/search", query_string={"q": "fixture", "page": page})
        assert response.status_code == 200 and response.json["page"] == page
    record.assert_called_once_with("fixture")


def test_search_history_write_failure_preserves_search_results(client, monkeypatch):
    monkeypatch.setattr(api_search, "search_albums", lambda **_: {
        "items": [{"title": "fixture"}], "total": 1, "has_more": False, "page": 1,
    })
    record = Mock(side_effect=OperationalError("fixture write error"))
    warning = Mock()
    monkeypatch.setattr(api_search, "add_search_history", record)
    monkeypatch.setattr(api_search.log, "warning", warning)

    response = client.get("/api/search", query_string={"q": "fixture"})

    assert response.status_code == 200
    assert response.json["items"] == [{"title": "fixture"}]
    record.assert_called_once_with("fixture")
    assert "OperationalError" in warning.call_args.args[0]
