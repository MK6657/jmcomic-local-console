"""资源库 “点击卡片空白处打开详情” setting and the shared 收藏 / 资源库 status-filter order."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ─── 资源库 card blank-space click ───

def test_card_click_is_off_by_default(client):
    assert client.get("/api/settings").get_json()["settings"]["library_card_click"] == "false"
    html = client.get("/library").get_data(as_text=True)
    assert re.search(r'<div id="library-grid" class="library-grid" data-nav-memory-list\s+data-card-click="false">', html)
    assert "library-grid--card-click" not in html
    settings = client.get("/settings").get_data(as_text=True)
    switch = re.search(r'<input type="checkbox" id="library_card_click" name="library_card_click"[^>]*>', settings).group(0)
    assert 'role="switch"' in switch and "checked" not in switch
    assert '<label class="form-check-label" for="library_card_click">点击资源库卡片空白处打开详情</label>' in settings


def test_card_click_setting_is_saved_and_reaches_the_library_page(client):
    saved = client.post("/api/settings", json={"library_card_click": True})
    assert saved.status_code == 200 and saved.get_json()["settings"]["library_card_click"] == "true"
    html = client.get("/library").get_data(as_text=True)
    assert re.search(r'<div id="library-grid" class="library-grid library-grid--card-click" data-nav-memory-list\s+'
                     r'data-card-click="true">', html)
    switch = re.search(r'<input type="checkbox" id="library_card_click"[^>]*>', client.get("/settings").get_data(as_text=True))
    assert "checked" in switch.group(0)
    client.post("/api/settings", json={"library_card_click": "false"})
    assert 'data-card-click="false"' in client.get("/library").get_data(as_text=True)


@pytest.mark.parametrize("value", ["yes", "1", 1, None, ""])
def test_card_click_setting_only_accepts_true_or_false(client, value):
    response = client.post("/api/settings", json={"library_card_click": value})
    assert response.status_code == 400
    assert client.get("/api/settings").get_json()["settings"]["library_card_click"] == "false"


def test_card_click_setting_survives_export_and_import(client):
    import io
    import json
    client.post("/api/settings", json={"library_card_click": "true"})
    exported = client.get("/api/settings/export")
    data = json.loads(exported.get_data(as_text=True))
    settings = data.get("settings", data)
    assert settings["library_card_click"] == "true"
    client.post("/api/settings", json={"library_card_click": "false"})
    upload = {"file": (io.BytesIO(exported.get_data()), "settings.json")}
    assert client.post("/api/settings/import", data=upload, content_type="multipart/form-data").status_code == 200
    assert client.get("/api/settings").get_json()["settings"]["library_card_click"] == "true"


def test_library_script_only_opens_details_from_blank_space_when_enabled():
    src = (ROOT / "static" / "js" / "library.js").read_text(encoding="utf-8")
    assert "var cardClickOpens = grid.dataset.cardClick === 'true';" in src
    handler = src[src.index("grid.addEventListener('click'"):]
    handler = handler[:handler.index("});") + 3]
    # the blank-space branch checks the setting before it can open a tab; explicit actions come first
    assert handler.index("filter-author") < handler.index("if (!cardClickOpens) return;") < handler.index("window.open(")
    # a changed setting is picked up when returning to the tab or from the back/forward cache
    assert "refreshCardClickSetting();" in src[src.index("window.addEventListener('pageshow'"):]
    visibility = src[src.index("document.addEventListener('visibilitychange'"):]
    assert "refreshCardClickSetting();" in visibility[:visibility.index("});")]


# ─── shared status-filter order ───

def _options(template, select_id):
    html = (ROOT / "templates" / template).read_text(encoding="utf-8")
    select = re.search(r'<select id="%s".*?</select>' % select_id, html, re.S).group(0)
    return re.findall(r'<option value="([^"]*)">', select)


# the favourites / library filter keys for the same group (library calls never-downloaded "undownloaded")
SAME_GROUP = {"": "", "readable": "readable", "active": "active", "failed": "failed", "missing": "missing",
              "none": "undownloaded"}


def test_favourites_and_library_list_the_status_groups_in_the_same_order():
    wishlist = _options("wishlist.html", "wishlist-status")
    library = _options("library.html", "library-status")
    assert wishlist == ["", "readable", "active", "failed", "missing", "none"]
    assert [SAME_GROUP[v] for v in wishlist] == library


def test_the_filter_order_matches_the_status_sort_order():
    from core import database as db
    order = db.WISHLIST_SORTS["status"]
    ranks = [order.index("'%s'" % group) for group in ("readable", "active", "failed", "missing")]
    assert ranks == sorted(ranks)
    assert _options("wishlist.html", "wishlist-status")[1:5] == ["readable", "active", "failed", "missing"]
