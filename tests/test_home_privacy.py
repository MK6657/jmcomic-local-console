import pytest

from core import database as db


@pytest.mark.parametrize("edition", ["atelier", "classic"])
def test_home_only_sends_download_history_after_opt_in(client, edition):
    client.set_cookie("jmconsole_edition", edition)
    db.insert_job("test-recent", "123", "Private completed title", [])
    assert db.transition_job_status("test-recent", ["queued"], "completed")
    db.insert_job("test-running", "124", "Active title", [])
    assert db.transition_job_status("test-running", ["queued"], "running")

    for preference in (None, True, False):
        if preference is not None:
            assert client.post("/api/settings", json={"home_show_recent_downloads": preference}).status_code == 200
        page = client.get("/")
        assert page.headers["Cache-Control"] == "no-store"
        assert b"Active title" in page.data
        assert (b"Private completed title" in page.data) == bool(preference)
        assert (b'id="home-recent-downloads"' in page.data) == bool(preference)


def test_history_pages_start_hidden_and_stale_page_cannot_fetch_history(client):
    db.add_search_history("test private keyword")
    page = client.get("/search")
    assert page.headers["Cache-Control"] == "no-store"
    assert b'aria-expanded="false"' in page.data
    assert b"test private keyword" not in page.data

    head = client.head("/api/search-history")
    session = head.headers["X-JM-History-Session"]
    assert session and session.encode() in page.data
    assert head.headers["Cache-Control"] == "no-store"
    assert not head.data
    stale = client.get("/api/search-history", headers={"X-JM-History-Session": "old"})
    assert stale.json["items"] == [] and stale.json["session"] == session
    current = client.get("/api/search-history", headers={"X-JM-History-Session": session})
    assert current.json["items"][0]["keyword"] == "test private keyword"
