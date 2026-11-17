"""Bounded job history, stable ordering and page-local filesystem work."""
from contextlib import closing
from unittest.mock import Mock

import pytest


def seed_jobs(db, count=330):
    states = ("running", "paused", "queued", "completed", "failed", "canceled")
    with closing(db.get_db()) as connection:
        with connection:
            connection.executemany(
                "INSERT INTO jobs(job_id,album_id,status,output_path,created_at) VALUES(?,?,?,?,?)",
                [(f"page_{i:04d}", str(900000 + i), states[i % len(states)], f"/unused/{i}", "2026-11-17T12:00:00")
                 for i in range(count)],
            )


def test_default_page_is_bounded_and_only_enriches_visible_paths(client, monkeypatch):
    from core import database as db
    from routes import api_jobs
    seed_jobs(db)
    scan = Mock(return_value=None)
    monkeypatch.setattr(api_jobs, "_check_cbz_path", scan)
    payload = client.get("/api/jobs").get_json()
    assert (len(payload["jobs"]), payload["total"], payload["page_size"], payload["pages"]) == (50, 330, 50, 7)
    assert scan.call_count == 50
    assert payload["counts"]["active"] == 110
    assert payload["counts"]["completed"] == 55
    assert payload["counts"]["all"] == 330
    assert [job["job_id"] for job in payload["jobs"]] == [f"page_{i:04d}" for i in range(329, 279, -1)]


@pytest.mark.parametrize("status,expected", [("active", 110), ("completed", 55), ("failed", 55), ("queued", 55), ("canceled", 55)])
def test_status_filter_has_global_counters_and_correct_page(client, status, expected):
    from core import database as db
    seed_jobs(db)
    data = client.get(f"/api/jobs?status={status}&page=2&page_size=20").get_json()
    allowed = {"running", "paused"} if status == "active" else {status}
    assert data["total"] == expected and len(data["jobs"]) == 20
    assert data["page"] == 2 and all(job["status"] in allowed for job in data["jobs"])
    assert data["counts"]["all"] == 330


def test_equal_timestamps_do_not_duplicate_or_skip_rows_between_pages(client):
    from core import database as db
    seed_jobs(db, 230)
    ids = []
    for page in range(1, 6):
        data = client.get(f"/api/jobs?page={page}").get_json()
        ids.extend(job["job_id"] for job in data["jobs"])
    assert len(ids) == len(set(ids)) == 230
    assert ids == [f"page_{i:04d}" for i in range(229, -1, -1)]


@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=abc", "page=1.5", "page_size=0", "page_size=-5",
                                   "page_size=oops", "page=99999999999999999", "status=completed;DROP TABLE jobs"])
def test_invalid_pagination_is_json_400_without_scanning_files(client, monkeypatch, query):
    from routes import api_jobs
    scan = Mock(side_effect=AssertionError("invalid query must not scan"))
    monkeypatch.setattr(api_jobs, "_check_cbz_path", scan)
    response = client.get("/api/jobs?" + query)
    assert response.status_code == 400 and response.get_json()["status"] == "error"
    scan.assert_not_called()


def test_page_size_is_capped_and_out_of_range_page_recovers_after_deletion(client):
    from core import database as db
    seed_jobs(db, 230)
    limited = client.get("/api/jobs?page_size=9999").get_json()
    assert len(limited["jobs"]) == limited["page_size"] == 200
    with closing(db.get_db()) as connection:
        with connection:
            connection.execute("DELETE FROM jobs WHERE id>45")
    data = client.get("/api/jobs?page=5").get_json()
    assert (data["page"], data["pages"], data["total"]) == (1, 1, 45)
    assert len(data["jobs"]) == 45


def test_empty_history_keeps_page_one_and_zero_counters(client):
    payload = client.get("/api/jobs?page=2&status=failed").get_json()
    assert (payload["page"], payload["pages"], payload["total"], payload["jobs"]) == (1, 1, 0, [])
    assert all(count == 0 for count in payload["counts"].values())


def test_old_completed_page_does_not_claim_its_job_is_latest(client):
    from core import database as db
    with closing(db.get_db()) as connection:
        with connection:
            connection.executemany("INSERT INTO jobs(job_id,album_id,status,created_at) VALUES(?,?,'completed',?)", [
                ("old", "123", "2026-11-16"), ("new", "123", "2026-11-17")])
    old = client.get("/api/jobs?status=completed&page_size=1&page=2").get_json()["jobs"][0]
    assert old["job_id"] == "old" and old["latest_completed_job_id"] == "new"


def test_counts_and_rows_share_a_snapshot_when_a_writer_commits_between_them(client, monkeypatch):
    from core import database as db
    seed_jobs(db, 2)
    get_db = db.get_db
    connection = get_db()
    class BetweenQueries:
        def execute(self, sql, params=()):
            if sql.startswith("SELECT j.*"):
                with closing(get_db()) as writer:
                    with writer:
                        writer.execute("INSERT INTO jobs(job_id,album_id,status) VALUES('late','990000','queued')")
            return connection.execute(sql, params)
        def close(self):
            connection.close()
    monkeypatch.setattr(db, "get_db", BetweenQueries)
    data = db.get_jobs_page()
    assert data["total"] == len(data["jobs"]) == 2
    assert all(job["job_id"] != "late" for job in data["jobs"])
