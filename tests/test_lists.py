"""Favourites (wishlist) and library lists: download-status filter, sorting, author filter,
the shared "readable offline" flag, whitelists, and no inline handlers built from remote data."""
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"
TEMPLATES = ROOT / "templates"


@pytest.fixture
def downloads(client, tmp_path, monkeypatch):
    from core import path_guard
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    return root


def _album(folder):
    """A downloaded album folder with one page: an empty folder is not readable offline."""
    folder.mkdir(parents=True)
    (folder / "001.webp").write_bytes(b"page")
    return folder


def _job(job_id, album_id, status, output_path=None, created_at=None):
    from core import database as db
    db.insert_job(job_id, album_id, "t", [])
    db.update_job(job_id, status=status, output_path=str(output_path) if output_path else None)
    if created_at:
        _sql("UPDATE jobs SET created_at=? WHERE job_id=?", (created_at, job_id))


def _sql(statement, params=()):
    from core import database as db
    conn = db.get_db()
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _fav(album_id, title="", author="", added_at="2026-01-01T00:00:00", status=None):
    from core import database as db
    db.add_wishlist(album_id, title, author, "")
    _sql("UPDATE wishlist SET added_at=? WHERE album_id=?", (added_at, album_id))
    if status is not None:
        _sql("UPDATE wishlist SET download_status=? WHERE album_id=?", (status, album_id))


def _ids(response):
    return [item["album_id"] for item in response.get_json()["items"]]


@pytest.fixture
def favourites(downloads):
    """One favourite per interesting case; the comment is the expected download-status group."""
    ok = _album(downloads / "ok")
    _fav("101", "Beta", "Zed", "2026-01-01T00:00:01")                    # readable
    _job("j101", "101", "completed", ok)
    _fav("102", "alpha", "", "2026-01-01T00:00:02")                      # none; author from album_meta
    _fav("103", "", "", "2026-01-01T00:00:03", status="")                # none; empty title/author, legacy ''
    _fav("104", "Queued one", "Amy", "2026-01-01T00:00:04", status="queued")   # active (queued job)
    _job("j104", "104", "queued")
    _fav("105", "Running one", "amy", "2026-01-01T00:00:05")             # active (running job)
    _job("j105", "105", "running")
    _fav("106", "Failed one", "Bob", "2026-01-01T00:00:06", status="failed")   # failed
    _job("j106", "106", "failed")
    _fav("107", "Gone", "Bob", "2026-01-01T00:00:07", status="completed")      # missing filter, files missing
    _job("j107", "107", "completed", downloads / "deleted")
    _fav("108", "Legacy dl", "Cat", "2026-01-01T00:00:08", status="downloading")  # active (no jobs)
    _fav("109", "Legacy fail", "Cat", "2026-01-01T00:00:09", status="failed")     # failed (no jobs)
    _fav("110", "Legacy done", "Cat", "2026-01-01T00:00:10", status="completed")  # missing filter, files missing
    _fav("111", "Read+queued", "Dan", "2026-01-01T00:00:11", status="queued")     # readable (+ queued)
    _job("j111a", "111", "completed", ok, created_at="2026-01-02T00:00:00")
    _job("j111b", "111", "queued", created_at="2026-01-03T00:00:00")
    _fav("112", "Read then failed", "Dan", "2026-01-01T00:00:12", status="failed")  # readable
    _job("j112a", "112", "completed", ok, created_at="2026-01-02T00:00:00")
    _job("j112b", "112", "failed", created_at="2026-01-03T00:00:00")
    _fav("113", "Failed then canceled", "Eve", "2026-01-01T00:00:13", status="none")  # none (latest canceled)
    _job("j113a", "113", "failed", created_at="2026-01-02T00:00:00")
    _job("j113b", "113", "canceled", created_at="2026-01-03T00:00:00")
    _fav("114", "Odd legacy", "Eve", "2026-01-01T00:00:14", status=" QUEUED ")    # active (no jobs)
    _fav("115", "Paused one", "Eve", "2026-01-01T00:00:15", status="downloading")  # active (paused job)
    _job("j115", "115", "paused")
    from core import database as db
    db.upsert_album_meta("102", author="Meta Author")


EXPECTED_GROUPS = {
    "readable": {"101", "111", "112"},
    "active": {"104", "105", "108", "114", "115"},
    "failed": {"106", "109"},
    "missing": {"107", "110"},
    "none": {"102", "103", "113"},
}


# ─── Favourites: download-status filter ───


def test_wishlist_status_filter_partitions_every_favourite(client, favourites):
    everything = client.get("/api/wishlist").get_json()
    assert everything["total"] == 15
    assert everything["group_counts"] == {k: len(v) for k, v in EXPECTED_GROUPS.items()}
    for group, expected in EXPECTED_GROUPS.items():
        data = client.get(f"/api/wishlist?status={group}").get_json()
        assert set(_ids_of(data)) == expected, group
        assert data["total"] == len(expected)  # pagination total reflects the filter
        assert data["applied"]["status"] == group
        assert all(item["status_group"] == ("none" if group == "missing" else group)
                   and item["files_missing"] == (group == "missing")
                   for item in data["items"] if group in ("missing", "none"))
        if group not in ("missing", "none"):
            assert all(item["status_group"] == group for item in data["items"])


def _ids_of(data):
    return [item["album_id"] for item in data["items"]]


def test_wishlist_items_carry_readable_flag_activity_and_missing_files(client, favourites):
    items = {i["album_id"]: i for i in client.get("/api/wishlist?page_size=200").get_json()["items"]}
    assert {a for a, i in items.items() if i["readable"]} == EXPECTED_GROUPS["readable"]
    assert items["111"]["activity"] == "queued"          # readable, and a new download is queued
    assert items["112"]["activity"] is None
    assert items["104"]["activity"] == "queued"
    assert items["105"]["activity"] == "downloading"
    assert items["115"]["activity"] == "paused"
    assert items["108"]["activity"] == "downloading"     # legacy column, no job rows
    assert items["114"]["activity"] == "queued"
    assert {a for a, i in items.items() if i["files_missing"]} == {"107", "110"}
    assert items["102"]["author"] == "Meta Author"       # empty favourite author filled from album_meta
    assert items["103"]["title"] == "" and items["103"]["download_status"] == "none"


def test_wishlist_readable_uses_the_shared_rule(client, favourites, downloads):
    from core.local_availability import readable_album_ids
    ids = [str(n) for n in range(101, 116)]
    flagged = {i["album_id"] for i in client.get("/api/wishlist?page_size=200").get_json()["items"] if i["readable"]}
    assert flagged == readable_album_ids(ids)
    shutil.rmtree(downloads / "ok")  # folder deleted after download: nothing is readable any more
    data = client.get("/api/wishlist?status=readable").get_json()
    assert data["total"] == 0 and data["group_counts"]["readable"] == 0
    assert {i["album_id"] for i in client.get("/api/wishlist?status=missing&page_size=200").get_json()["items"]} >= {"101"}


def test_wishlist_keyword_combines_with_status_and_escapes_wildcards(client, favourites):
    data = client.get("/api/wishlist", query_string={"q": "legacy", "status": "failed"}).get_json()
    assert _ids_of(data) == ["109"] and data["total"] == 1
    # "Legacy dl" + "Odd legacy" / "Legacy fail" / "Legacy done": counts follow the keyword
    assert data["group_counts"] == {"readable": 0, "active": 2, "failed": 1, "missing": 1, "none": 0}
    assert _ids_of(client.get("/api/wishlist", query_string={"q": "meta author"}).get_json()) == ["102"]
    _fav("120", "100% pure", "x", "2026-01-01T00:00:20")
    assert _ids_of(client.get("/api/wishlist", query_string={"q": "%"}).get_json()) == ["120"]
    assert client.get("/api/wishlist", query_string={"q": "_"}).get_json()["total"] == 0


# ─── Favourites: sorting ───


def test_wishlist_sorts(client, favourites):
    def order(sort, **extra):
        return _ids(client.get("/api/wishlist", query_string={"sort": sort, "page_size": 200, **extra}))

    newest = order("added_at")
    assert newest[0] == "115" and newest[-1] == "101"
    assert order("added_asc") == list(reversed(newest))
    by_title = order("title")
    assert by_title[-1] == "103"                        # the only empty title goes last
    assert by_title[0] == "102"                         # "alpha" before "Beta": case-insensitive
    by_author = order("author")
    items ={i["album_id"]: i for i in client.get("/api/wishlist?page_size=200").get_json()["items"]}
    seen = [items[a]["author"] for a in by_author]
    assert seen[-1] == ""                               # empty author last
    non_empty = [a.lower() for a in seen if a]
    assert non_empty == sorted(non_empty)
    assert by_author.index("104") < by_author.index("105")   # same author (Amy/amy): then by title
    by_status = order("status")
    rank = {"readable": 0, "active": 1, "failed": 2, "missing": 3, "none": 4}
    groups = [("missing" if items[a]["files_missing"] else items[a]["status_group"]) for a in by_status]
    assert [rank[g] for g in groups] == sorted(rank[g] for g in groups)
    assert order("status", status="readable") == ["112", "111", "101"]  # newest first within a group


def test_wishlist_pagination_total_follows_filter(client, favourites):
    first = client.get("/api/wishlist?status=active&page_size=2&page=1").get_json()
    last = client.get("/api/wishlist?status=active&page_size=2&page=3").get_json()
    assert first["total"] == last["total"] == 5
    assert len(first["items"]) == 2 and len(last["items"]) == 1
    assert client.get("/api/wishlist?status=active&page_size=2&page=4").get_json()["items"] == []


def test_wishlist_whitelists_reject_unknown_sort_and_status(client, favourites):
    from core import database as db
    default = _ids(client.get("/api/wishlist?page_size=200"))
    for bad in ("title; DROP TABLE wishlist", "id", "added_at DESC", "TITLE", "status--"):
        data = client.get("/api/wishlist", query_string={"sort": bad, "page_size": 200}).get_json()
        assert data["applied"]["sort"] == "added_at"
        assert _ids_of(data) == default
    for bad in ("x", "readable' OR 1=1 --", "READABLE", "completed"):
        data = client.get("/api/wishlist", query_string={"status": bad}).get_json()
        assert data["applied"]["status"] == "" and data["total"] == 15
    assert db.get_all_wishlist(sort="1; DROP TABLE wishlist", status="bogus")["total"] == 15
    assert db.get_wishlist("101") is not None


# ─── Library: readable flag, author filter and sort ───


@pytest.fixture
def library(downloads):
    from core import database as db
    _album(downloads / "a")
    _fav("201", "Zeta", "Author A", "2026-01-01T00:00:01")
    _job("j201", "201", "completed", downloads / "a")        # favourite, readable
    _fav("202", "alpha", "author a ", "2026-01-01T00:00:02")  # same author (case/space), not downloaded
    _fav("203", "Mid", "", "2026-01-01T00:00:03")             # no author
    _job("j204", "204", "completed", downloads / "a")        # downloaded, not a favourite
    db.upsert_album_meta("204", title="Beta", author="Author B")
    _job("j205", "205", "completed", downloads / "gone")     # downloaded, files deleted
    db.upsert_album_meta("205", title="Gamma", author="Author B")
    db.add_album_tag("201", "shared", source="user")
    db.add_album_tag("204", "shared", source="user")


def test_library_items_have_readable_flag(client, library):
    items = {i["album_id"]: i for i in client.get("/api/library").get_json()["items"]}
    assert {a for a, i in items.items() if i["readable"]} == {"201", "204"}
    assert items["205"]["download_status"] == "completed" and items["205"]["file_exists"] is False
    assert client.get("/api/library/201").get_json()["item"]["readable"] is True
    assert client.get("/api/library/205").get_json()["item"]["readable"] is False


def test_library_readable_filter_and_stats(client, library):
    data = client.get("/api/library?status=readable").get_json()
    assert sorted(_ids_of(data)) == ["201", "204"] and data["total"] == 2
    assert all(i["readable"] for i in data["items"])
    stats = client.get("/api/library/stats").get_json()
    assert stats["readable_count"] == 2 and stats["downloaded_count"] == 3


def test_library_readable_filter_checks_more_than_one_batch(client, downloads):
    from core import database as db
    from core.local_availability import MAX_IDS
    _album(downloads / "many")
    conn = db.get_db()
    try:
        conn.executemany(
            "INSERT INTO jobs (job_id, album_id, status, output_path, created_at) VALUES (?, ?, 'completed', ?, ?)",
            [(f"m{n}", str(300000 + n), str(downloads / "many"), "2026-01-01") for n in range(MAX_IDS + 5)],
        )
        conn.commit()
    finally:
        conn.close()
    data = client.get("/api/library?status=readable&page_size=10").get_json()
    assert data["total"] == MAX_IDS + 5
    assert client.get("/api/library/stats").get_json()["readable_count"] == MAX_IDS + 5


def test_library_author_filter(client, library):
    data = client.get("/api/library", query_string={"author": "  AUTHOR a"}).get_json()
    assert sorted(_ids_of(data)) == ["201", "202"] and data["total"] == 2
    assert data["applied"]["author"] == "AUTHOR a"
    both = client.get("/api/library", query_string={"author": "Author A", "tag": "shared"}).get_json()
    assert _ids_of(both) == ["201"]                       # combines with the tag filter (AND)
    paged = client.get("/api/library", query_string={"author": "Author B", "page_size": 1, "page": 2}).get_json()
    assert paged["total"] == 2 and len(paged["items"]) == 1
    assert client.get("/api/library", query_string={"author": "Nobody"}).get_json()["total"] == 0
    assert client.get("/api/library", query_string={"author": "%"}).get_json()["total"] == 0  # exact, not LIKE
    assert client.get("/api/library", query_string={"author": ""}).get_json()["total"] == 5   # empty = no filter


def test_library_sort_by_author_then_title_empty_last(client, library):
    assert _ids(client.get("/api/library?sort=author")) == ["202", "201", "204", "205", "203"]


def test_library_whitelists(client, library):
    default = _ids(client.get("/api/library"))
    for bad in ("author; DROP TABLE jobs", "AUTHOR", "readable"):
        data = client.get("/api/library", query_string={"sort": bad}).get_json()
        assert data["applied"]["sort"] == "updated_at" and _ids_of(data) == default
    data = client.get("/api/library", query_string={"status": "readable'--"}).get_json()
    assert data["applied"]["status"] == "" and data["total"] == 5


def _state(item):
    return item["status_group"], item["activity"], item["files_missing"], item["readable"]


def test_library_status_agrees_with_favourites(client, favourites, downloads):
    # the library used to call every comic with a completed job "文件已删除" and ignore newer jobs,
    # while favourites showed 排队中 / 下载中 / 失败 for the same comic
    _fav("116", "Gone then queued", "Fay", "2026-01-01T00:00:16", status="queued")
    _job("j116a", "116", "completed", downloads / "deleted", created_at="2026-01-02T00:00:00")
    _job("j116b", "116", "queued", created_at="2026-01-03T00:00:00")
    _fav("117", "Gone then failed", "Fay", "2026-01-01T00:00:17", status="failed")
    _job("j117a", "117", "completed", downloads / "deleted", created_at="2026-01-02T00:00:00")
    _job("j117b", "117", "failed", created_at="2026-01-03T00:00:00")
    wishlist = {i["album_id"]: i for i in client.get("/api/wishlist?page_size=200").get_json()["items"]}
    library = {i["album_id"]: i for i in client.get("/api/library?page_size=200").get_json()["items"]}
    assert set(wishlist) <= set(library)
    for album_id, favourite in wishlist.items():
        assert _state(library[album_id]) == _state(favourite), album_id
        assert "_facts" not in library[album_id]
    assert _state(library["116"]) == ("active", "queued", False, False)
    assert _state(library["117"]) == ("failed", None, False, False)
    assert _state(library["107"]) == ("none", None, True, False)
    assert _state(library["111"]) == ("readable", "queued", False, True)
    one = client.get("/api/library/116").get_json()["item"]
    assert _state(one) == ("active", "queued", False, False) and "_facts" not in one


def test_library_status_for_downloads_that_are_not_favourites(client, library):
    _job("j205b", "205", "running", created_at="2099-01-01T00:00:00")  # files gone, re-downloading now
    items = {i["album_id"]: i for i in client.get("/api/library").get_json()["items"]}
    assert _state(items["204"]) == ("readable", None, False, True)
    assert _state(items["205"]) == ("active", "downloading", False, False)
    assert _state(items["202"]) == ("none", None, False, False)


def test_library_labels_completed_jobs_as_downloaded_before(client):
    # "已下载" for a comic whose files are gone contradicted favourites' "未下载" for the same comic
    html = client.get("/library").get_data(as_text=True)
    assert '<option value="missing">下载过 · 本地文件不可用</option>' in html
    assert re.search(r"下载过 <strong id=\"stat-downloaded\">", html)
    assert "已下载" not in html.replace("已下载 · 可离线阅读", "")
    source = (STATIC_JS / "library.js").read_text(encoding="utf-8")
    assert "item.status_group" in source and "item.files_missing" in source and "item.activity" in source


# ─── Library: download-status buckets ───

# Library bucket → shared status_group and files_missing (the favourites filter splits them too)
LIBRARY_BUCKETS = {
    "readable": ("readable", False), "active": ("active", False), "failed": ("failed", False),
    "missing": ("none", True), "undownloaded": ("none", False),
}


@pytest.fixture
def mixed_library(favourites, downloads):
    """The favourites fixture plus downloads that are not favourites."""
    from core import database as db
    ok = downloads / "ok"
    for album_id in ("101", "106", "302"):
        db.add_album_tag(album_id, "shared", source="user")
    _job("j301", "301", "completed", ok)                                          # readable
    _job("j302", "302", "completed", downloads / "deleted")                       # files deleted
    _job("j303a", "303", "completed", downloads / "deleted", created_at="2026-01-02T00:00:00")
    _job("j303b", "303", "running", created_at="2026-01-03T00:00:00")           # files deleted, downloading again
    _job("j304a", "304", "completed", ok, created_at="2026-01-02T00:00:00")
    _job("j304b", "304", "failed", created_at="2026-01-03T00:00:00")            # readable, newer job failed
    _job("j305a", "305", "completed", downloads / "deleted", created_at="2026-01-02T00:00:00")
    _job("j305b", "305", "failed", created_at="2026-01-03T00:00:00")            # files deleted, retry failed


EXPECTED_BUCKETS = {
    "readable": {"101", "111", "112", "301", "304"},
    "active": {"104", "105", "108", "114", "115", "303"},
    "failed": {"106", "109", "305"},
    "missing": {"107", "110", "302"},
    "undownloaded": {"102", "103", "113"},
}


def test_library_status_buckets_partition_the_library(client, mixed_library):
    # failed and queued/downloading comics used to be in no bucket but 全部
    from core import database as db
    assert tuple(EXPECTED_BUCKETS) == db.LIBRARY_STATUS_FILTERS
    everything = client.get("/api/library?page_size=200").get_json()
    all_ids = set(_ids_of(everything))
    assert everything["total"] == len(all_ids) == 20
    seen = set()
    for bucket, expected in EXPECTED_BUCKETS.items():
        data = client.get(f"/api/library?status={bucket}&page_size=200").get_json()
        ids = set(_ids_of(data))
        assert ids == expected, bucket
        assert data["total"] == len(expected) and data["applied"]["status"] == bucket
        group, missing = LIBRARY_BUCKETS[bucket]
        assert all((i["status_group"], i["files_missing"]) == (group, missing) for i in data["items"]), bucket
        assert not ids & seen, bucket  # no comic in two buckets
        seen |= ids
    assert seen == all_ids  # every comic is in some bucket


def test_library_buckets_match_the_favourites_groups(client, mixed_library):
    library = {}
    for bucket in EXPECTED_BUCKETS:
        for album_id in _ids_of(client.get(f"/api/library?status={bucket}&page_size=200").get_json()):
            library[album_id] = LIBRARY_BUCKETS[bucket][0]
    for group in EXPECTED_GROUPS:
        favourites = set(_ids_of(client.get(f"/api/wishlist?status={group}&page_size=200").get_json()))
        assert favourites == EXPECTED_GROUPS[group]
        shared_group = "none" if group == "missing" else group
        assert {a for a in favourites if library[a] != shared_group} == set(), group


def test_library_favourites_without_jobs_follow_the_status_column(client, mixed_library):
    # 未下载 used to be download_status = 'none' only: a favourite whose column says queued/failed but that has
    # no job at all was in no bucket. It goes where favourites (and its own card badge) put it.
    def bucket_of(album_id):
        return [b for b in EXPECTED_BUCKETS
                if album_id in _ids_of(client.get(f"/api/library?status={b}&page_size=200").get_json())]
    items = {i["album_id"]: i for i in client.get("/api/library?page_size=200").get_json()["items"]}
    assert bucket_of("108") == ["active"] and items["108"]["activity"] == "downloading"   # column 'downloading'
    assert bucket_of("114") == ["active"] and items["114"]["activity"] == "queued"        # column ' QUEUED '
    assert bucket_of("109") == ["failed"]                                                  # column 'failed'
    assert bucket_of("110") == ["missing"] and items["110"]["files_missing"]              # column 'completed'
    assert bucket_of("113") == ["undownloaded"]    # column 'none'; latest job canceled after a failure


def test_library_bucket_pagination_and_other_filters(client, mixed_library):
    first = client.get("/api/library?status=active&page_size=4&page=1").get_json()
    second = client.get("/api/library?status=active&page_size=4&page=2").get_json()
    assert first["total"] == second["total"] == 6
    assert len(first["items"]) == 4 and len(second["items"]) == 2
    assert not set(_ids_of(first)) & set(_ids_of(second))
    assert set(_ids_of(first)) | set(_ids_of(second)) == EXPECTED_BUCKETS["active"]
    data = client.get("/api/library", query_string={"q": "legacy", "status": "failed"}).get_json()
    assert _ids_of(data) == ["109"] and data["total"] == 1
    data = client.get("/api/library", query_string={"author": "cat", "status": "missing"}).get_json()
    assert _ids_of(data) == ["110"] and data["total"] == 1
    data = client.get("/api/library", query_string={"status": "readable", "tag": "shared"}).get_json()
    assert _ids_of(data) == ["101"] and data["total"] == 1


def test_library_status_aliases_and_whitelist(client, mixed_library):
    def ids(status):
        data = client.get("/api/library", query_string={"status": status, "page_size": 200}).get_json()
        return data["applied"]["status"], set(_ids_of(data))
    assert ids("none") == ("undownloaded", EXPECTED_BUCKETS["undownloaded"])  # favourites' value for 未下载
    assert ids("queued") == ("active", EXPECTED_BUCKETS["active"])            # old value, a subset of active
    everything = set().union(*EXPECTED_BUCKETS.values())
    for bad in ("downloaded", "completed", "MISSING", "failed' OR 1=1 --", "none none"):
        assert ids(bad) == ("", everything), bad  # overlapping / unknown values: no filter


def test_library_status_select_offers_the_buckets(client):
    html = client.get("/library").get_data(as_text=True)
    select = html[html.index('id="library-status"'):]
    select = select[:select.index("</select>")]
    options = re.findall(r'<option value="([^"]*)">([^<]*)</option>', select)
    assert options == [("", "状态：全部"), ("readable", "可离线阅读"), ("active", "排队中 / 下载中"),
                       ("failed", "失败"), ("missing", "下载过 · 本地文件不可用"), ("undownloaded", "未下载")]
    wishlist = client.get("/wishlist").get_data(as_text=True)
    for value, label in (("active", "排队中 / 下载中"), ("failed", "失败"), ("none", "未下载")):
        assert f'<option value="{value}">{label}</option>' in wishlist  # same wording on both pages


def test_library_query_count_unchanged_with_author_filter(client, library, monkeypatch):
    from unittest.mock import Mock
    from core import database as db
    original = db.get_db
    statements = []

    def connect():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn
    counted = Mock(side_effect=connect)
    monkeypatch.setattr(db, "get_db", counted)
    result = db.get_library(author="Author B", sort="author")
    assert [i["album_id"] for i in result["items"]] == ["204", "205"]
    assert counted.call_count == 1 and len(statements) == 4


# ─── Static checks: no inline handlers built from remote/user data ───


INLINE_HANDLER = re.compile(r"""\bon(click|error|change|input|keydown|keyup|submit|load|mouse\w+|focus|blur)\s*=""", re.I)


@pytest.mark.parametrize("script", ["library.js", "wishlist.js"])
def test_list_scripts_have_no_inline_handlers(script):
    source = (STATIC_JS / script).read_text(encoding="utf-8")
    assert not INLINE_HANDLER.search(source)
    assert not re.search(r"setAttribute\(\s*['\"]on", source)
    # innerHTML only ever receives a fixed string literal (spinner / empty state), never data
    for line in re.findall(r"\.innerHTML\s*=\s*(.+)", source):
        assert re.fullmatch(r"'[^'+]*';", line.strip()), line
    assert "escapeHtml" not in source  # everything is built with textContent / dataset instead
    assert "dataset.action" in source  # clicks are delegated through data-action


@pytest.mark.parametrize("script, reload", [
    ("wishlist.js", "loadWishlist(currentPage);"),
    ("library.js", "loadLibrary(currentPage, { showSpinner: false });"),
])
def test_list_pages_resume_after_back_forward_cache(script, reload):
    # leaving through 阅读 (same tab) used to kill the auto-refresh for good: nothing restarted it on Back
    source = (STATIC_JS / script).read_text(encoding="utf-8")
    assert "beforeunload" not in source
    assert "addEventListener('pagehide', stopAutoRefresh)" in source
    assert "addEventListener('visibilitychange'" in source
    handler = source[source.index("addEventListener('pageshow'"):source.index("addEventListener('visibilitychange'")]
    assert "event.persisted" in handler and reload in handler and "startAutoRefresh();" in handler


def test_detail_links_from_lists_keep_a_same_origin_referrer():
    # noreferrer left the detail page's 返回 with nothing to go back to but an empty /search
    for script in ("library.js", "wishlist.js"):
        source = (STATIC_JS / script).read_text(encoding="utf-8")
        assert "noreferrer" not in source and "noopener" in source
    detail = (STATIC_JS / "detail.js").read_text(encoding="utf-8")
    back = detail[detail.index("// ── 返回 ──"):detail.index("function setOfflineStatus")]
    assert "document.referrer" in back and "'/library'" in back and "'/wishlist'" in back
    assert "ref.origin !== location.origin" in back
    nav = (STATIC_JS / "reading-nav.js").read_text(encoding="utf-8")
    assert "navigation.canGoBack" in nav  # a tab opened from a list has history but nothing behind it


def test_files_missing_badge_is_legible():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    rules = " ".join(re.findall(r"\.status-badge-muted\s*\{([^}]*)\}", css))
    assert rules and "opacity" not in rules
    assert "var(--text-secondary)" in rules and "var(--bg-tertiary)" in rules
    tokens = dict(re.findall(r"(--[a-z-]+):\s*(#[0-9A-Fa-f]{6});", css[:css.index("}")]))

    def luminance(hex_colour):
        channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    fg, bg = luminance(tokens["--text-secondary"]), luminance(tokens["--bg-tertiary"])
    assert (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05) >= 4.5  # WCAG AA for 12px text
    for script in ("library.js", "wishlist.js", "downloads.js"):
        source = (STATIC_JS / script).read_text(encoding="utf-8")
        assert "bg-secondary status-badge-muted" not in source  # white on grey at 0.7 opacity was ~2.2:1


def test_wishlist_row_layout_survives_narrow_tables():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    # buttons wrap as whole buttons (阅读 used to stack one character per line)
    assert re.search(r"\.wishlist-actions \.btn\s*\{[^}]*white-space:\s*nowrap", css)
    source = (STATIC_JS / "wishlist.js").read_text(encoding="utf-8")
    row = source[source.index("function buildRow"):source.index("function actionButton")]
    # 阅读 comes last so 下载 / 详情 / 移除 line up on every row
    assert row.index("window.readLink.create(") > row.index("'remove'")


def _css_rule(css, selector):
    """Declarations of the last rule whose selector list is exactly `selector` (later rules win the cascade)."""
    blocks = re.findall(r"(?:^|[}/])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert blocks, selector
    return blocks[-1]


def test_wishlist_table_wraps_long_text_instead_of_widening():
    # 60 characters without spaces used to set the column's minimum width: the table grew to ~1500px, pushed
    # 下载状态 / 添加时间 / 操作 off screen, and stacked the action buttons three or four lines high
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    wrap = _css_rule(css, ".wishlist-table .wishlist-title,\n.wishlist-table .wishlist-author")
    assert re.search(r"overflow-wrap:\s*anywhere", wrap)  # break-word would not lower the min-content width
    assert re.search(r"min-width:\s*160px", _css_rule(css, ".wishlist-table .wishlist-title"))
    assert re.search(r"min-width:\s*96px", _css_rule(css, ".wishlist-table .wishlist-author"))
    assert "nowrap" in _css_rule(css, ".wishlist-table .wishlist-col-id")
    assert re.search(r"width:\s*15%", _css_rule(css, ".wishlist-table .wishlist-col-author"))
    # badges never break inside their label, and this rule comes after the old "white-space: normal"
    badges = ".wishlist-status .badge,\n.wishlist-status .offline-badge"
    assert "white-space: nowrap" in _css_rule(css, badges)
    assert css.index(badges) > css.index(".wishlist-status .offline-badge { white-space: normal; }")
    # the screen-reader text is positioned inside the badge, not against the page (it widened phones' pages)
    assert "position: relative" in _css_rule(css, ".wishlist-status .offline-badge")
    wide = css[css.rindex("@media (min-width: 992px)"):]
    wide = wide[:wide.index("\n}") + 2]
    assert re.search(r"\.wishlist-table \.wishlist-actions\s*\{\s*flex-wrap:\s*nowrap", wide)
    assert re.search(r"\.wishlist-table \.wishlist-col-actions\s*\{\s*width:\s*200px", wide)


def test_wishlist_table_markup_uses_the_layout_classes(client):
    html = client.get("/wishlist").get_data(as_text=True)
    table = html[html.index('<div class="table-responsive">'):html.index("</table>")]
    assert 'class="table table-hover align-middle wishlist-table" id="wishlist-table"' in table
    columns = re.findall(r'<th class="(wishlist-col-[a-z]+)"', table)
    assert columns == ["wishlist-col-" + c for c in ("check", "id", "title", "author", "status", "added", "actions")]
    assert "style=" not in table
    source = (STATIC_JS / "wishlist.js").read_text(encoding="utf-8")
    row = source[source.index("function buildRow"):source.index("function actionButton")]
    for column in ("check", "id", "title", "author", "status", "added", "actions"):
        assert f"'wishlist-col-{column}')" in row, column
    # title / author stay plain text inside their wrapping blocks
    assert "el('div', 'wishlist-title', item.title || '-')" in row
    assert "el('div', 'wishlist-author', item.author || '-')" in row
    badges = source[source.index("function statusBadges"):source.index("function activityBadge")]
    # short label in the table; the full wording stays in the title and the screen-reader text
    assert "el('span', 'visually-hidden', '已下载 · ')" in badges and "el('span', null, '可离线阅读')" in badges
    assert "offline.title = '已下载 · 可离线阅读" in badges
    assert "text-nowrap" not in badges


def test_library_card_author_keeps_icon_next_to_name():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    blocks = re.findall(r"\.library-card \.card-author\s*\{([^}]*)\}", css)
    assert any(re.search(r"display:\s*flex", b) for b in blocks)


def test_library_keeps_keyboard_focus_on_filters():
    source = (STATIC_JS / "library.js").read_text(encoding="utf-8")
    refresh = source[source.index("function canRefreshNow"):source.index("function refreshLibrary")]
    for area in ("grid", "activeFilters", "pagination"):
        assert area + ".contains(document.activeElement)" in refresh
    chips = source[source.index("function renderActiveFilters"):source.index("function loadStats")]
    assert "dataset.author === currentAuthor" in chips  # unchanged filter: the chip is not rebuilt
    author = source[source.index("function setAuthorFilter"):source.index("function renderActiveFilters")]
    assert ".focus(" in author  # focus moves to the new chip instead of dropping to <body>


@pytest.mark.parametrize("template", ["library.html", "wishlist.html"])
def test_list_templates_have_no_inline_handlers(template):
    assert not re.search(r"\son[a-z]+\s*=", (TEMPLATES / template).read_text(encoding="utf-8"))


def test_list_pages_render_without_inline_handlers(client):
    for path in ("/library", "/wishlist"):
        html = client.get(path).get_data(as_text=True)
        assert not re.search(r"\son[a-z]+\s*=\s*\"", html), path
    library = client.get("/library").get_data(as_text=True)
    assert '<option value="author">' in library and '<option value="readable">' in library
    assert 'id="library-active-filters"' in library and 'id="stat-readable"' in library
    wishlist = client.get("/wishlist").get_data(as_text=True)
    for value in ("none", "active", "readable", "failed"):
        assert f'<option value="{value}">' in wishlist
    for value in ("added_at", "added_asc", "title", "author", "status"):
        assert f'<option value="{value}">' in wishlist


def test_library_url_opens_the_bucket_of_a_status_alias():
    # /api/library?status=none|queued worked, but /library?status=none showed 全部 and rewrote the URL to /library:
    # the select has no none / queued option (browser check: scratch fixer/ui_checks.mjs, section 5)
    from routes.api_library import _STATUS_ALIASES
    source = (STATIC_JS / "library.js").read_text(encoding="utf-8")
    aliases = re.search(r"var STATUS_ALIASES = \{([^}]*)\};", source)
    assert aliases and dict(re.findall(r"(\w+): '(\w+)'", aliases.group(1))) == _STATUS_ALIASES
    read = source[source.index("function readUrlState"):source.index("function writeUrlState")]
    assert "setSelect(statusSelect, statusFromUrl(params.get('status') || ''), '')" in read


def test_star_toggles_keep_one_name_and_report_the_state_with_aria_pressed():
    # the label switched to 取消收藏 as well: "取消收藏, toggle button, pressed" reads as the unfavourite action being on
    for script, start in (("detail.js", "function setWishlistButton"), ("library.js", "function setStarState")):
        source = (STATIC_JS / script).read_text(encoding="utf-8")
        body = source[source.index(start):]
        body = body[:body.index("\n    }\n")]
        assert "btn.setAttribute('aria-label', '收藏');" in body, script
        assert "setAttribute('aria-label', btn.title)" not in body, script
        assert "btn.setAttribute('aria-pressed', starred ? 'true' : 'false');" in body, script
        assert "btn.title = starred ? '取消收藏' : '收藏';" in body, script  # the tooltip still says what a click does


def test_wishlist_table_fits_its_container_between_768_and_991px():
    # Bootstrap's container is 696px there but the table needed 775px: the whole 操作 column sat outside the scroll
    # box and its buttons stacked one per line (browser check at 991 / 960 / 768px: fixer/ui_checks.mjs, section 6)
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    mid = css[css.rindex("@media (max-width: 991.98px)"):]
    mid = mid[:mid.index("\n}") + 2]
    assert re.search(r"\.wishlist-table \.wishlist-col-added\s*\{\s*display:\s*none;", mid)
    assert re.search(r"\.wishlist-table \.wishlist-actions\s*\{\s*min-width:\s*100px;", mid)  # two buttons per line
    cells = re.search(r"\.wishlist-table > thead > tr > th,\s*\.wishlist-table > tbody > tr > td\s*\{([^}]*)\}", mid)
    assert cells and re.search(r"padding-left:\s*8px;", cells.group(1)) and re.search(r"padding-right:\s*8px;", cells.group(1))


def test_focused_and_pressed_buttons_and_table_text_stay_in_the_palette():
    # keyboard focus and a held mouse button read Bootstrap's --bs-btn-* variables: #198754, #0dcaf0 with a black icon,
    # #dc3545, and #ffc107 with a black star, each with a glow of its own colour instead of the primary blue
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    for selector, token in ((".btn-outline-success", "--success"), (".btn-outline-info", "--info"),
                            (".btn-outline-danger", "--error"), (".btn-outline-warning", "--warning"),
                            (".wishlist-btn.btn-warning", "--warning")):
        rules = re.findall(r"^" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.M)
        rule = next((r for r in rules if "--bs-btn-focus-shadow-rgb" in r), "")
        for var in ("hover-bg", "hover-border-color", "active-bg", "active-border-color"):
            assert re.search(rf"--bs-btn-{var}:\s*var\({token}\);", rule), (selector, var)
        for var in ("hover-color", "active-color"):
            assert re.search(rf"--bs-btn-{var}:\s*var\(--text-on-accent\);", rule), (selector, var)
        assert re.search(r"--bs-btn-focus-shadow-rgb:\s*59, 130, 246;", rule), selector
    # table cells take their colour from --bs-table-*color (Bootstrap: #000), not from the .table rule's color
    table = _css_rule(css, ".table")
    for var in ("color", "hover-color", "striped-color", "active-color"):
        assert re.search(rf"--bs-table-{var}:\s*var\(--text-primary\);", table), var


def test_wishlist_header_wraps_instead_of_breaking_the_title():
    # at 390px the button group squeezed the heading until it broke inside the word: 收藏清 / 单
    template = (TEMPLATES / "wishlist.html").read_text(encoding="utf-8")
    assert re.search(r'<div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">\s*'
                     r'<h4 class="text-nowrap"><i class="bi bi-bookmark-heart"></i> 收藏清单</h4>', template)
