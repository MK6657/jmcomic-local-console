"""批量下载（PR-B）in tests/ui_reader_fixture.py: its samples give the three lists the plan promises, and in the
fixture every download entry point only records a queued job.

The fixture's own seeding functions write to pytest's tmp database and a tmp downloads folder (asserted by the
downloads fixture). Its stubs go onto the app's real job manager and update checker inside a MonkeyPatch context,
and the real ones are back afterwards (asserted). No server is started, the network is blocked (conftest) and
nothing downloads. No core module is imported at collection time (core.logger opens files where the app root points then)."""
import importlib.util
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = ROOT / "tests" / "ui_reader_fixture.py"
PREVIEW = "/api/batch-downloads/preview"
CONFIRM = "/api/batch-downloads/confirm"


def _fixture_module():
    spec = importlib.util.spec_from_file_location("ui_reader_fixture_for_batch_tests", FIXTURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _forget_local():
    from core import archive_pages, local_availability
    archive_pages.clear_cache()
    with local_availability._cache_lock:
        local_availability._cache.clear()


def _dl_threads():
    return [t.name for t in threading.enumerate() if t.name.startswith("dl-") and t.is_alive()]


def _preview(client, kind, **body):
    response = client.post(PREVIEW, json={"kind": kind, **body})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _chapters(data):
    return {item["album_id"]: item["photo_ids"] for item in data["items"]}


def _reasons(data):
    return [(item["album_id"], item["reason"]) for item in data["skipped"]]


def _new_jobs(view):
    return [(job["album_id"], job["status"], job["selected_photo_ids"]) for job in view["new_jobs"]]


@pytest.fixture
def downloads(client, tmp_path, monkeypatch):
    from core import database as db, jm_service, path_guard, update_checker
    from routes import api_export, api_preview
    root = tmp_path / "downloads"
    root.mkdir()
    for module in (path_guard, api_preview, api_export, jm_service):
        monkeypatch.setattr(module, "DOWNLOAD_ROOT", root)
    assert Path(db.DB_PATH).resolve().is_relative_to(tmp_path.resolve())
    assert Path(path_guard.DOWNLOAD_ROOT).resolve().is_relative_to(tmp_path.resolve())
    _forget_local()
    update_checker._reset_for_tests()
    # install_update_stub assigns the module attribute directly: register it so teardown restores the real fetch
    monkeypatch.setattr(update_checker, "fetch_upstream_episodes", update_checker.fetch_upstream_episodes)
    monkeypatch.setattr(jm_service, "new_check_client",
                        lambda *a, **k: pytest.fail("the fixture reached the real check client"))
    yield root
    update_checker._reset_for_tests()
    _forget_local()
    assert not _dl_threads()


@contextmanager
def _job_stub(fixture):
    """The fixture's install_job_stub on the app's job manager; leaving the block puts the real ones back."""
    import core.job_manager as job_module
    from core import jm_service
    manager = job_module.job_manager
    real = job_module.download_album_job
    assert jm_service.download_album_job is real
    assert "_schedule_next" not in vars(manager) and "start" not in vars(manager)
    with pytest.MonkeyPatch.context() as patch:
        # install_job_stub assigns directly: register every name first so the context restores it
        patch.setattr(job_module, "download_album_job", real)
        patch.setattr(jm_service, "download_album_job", real)
        patch.setitem(vars(manager), "_schedule_next", None)   # not on the instance before: removed again after
        patch.setitem(vars(manager), "start", None)
        yield fixture.install_job_stub(manager)
    assert "_schedule_next" not in vars(manager) and "start" not in vars(manager)
    assert job_module.download_album_job is real and jm_service.download_album_job is real


def test_ui_fixture_batch_samples_seed_the_planned_targets(client, downloads):
    from PIL import Image, ImageDraw
    import core.job_manager as job_module
    from core import database as db, jm_service
    fixture = _fixture_module()
    details = fixture.seed_samples(downloads, db, Image, ImageDraw)
    assert set(details) == {"900010", "900011"}
    _forget_local()
    with _job_stub(fixture) as metrics:
        manager = job_module.job_manager
        assert manager._schedule_next is metrics["stub"] and job_module.download_album_job is metrics["refuse"]
        seeded = {job["job_id"] for job in db.get_all_jobs()}

        # 下载新章节: 4 comics / 5 话 (900022 organized by author is listed); skipped 900021 (queued job); 900009 for review
        new = _preview(client, "new_chapters")
        assert _chapters(new) == {"900011": ["91104"], "900020": ["92103", "92104"], "900500": ["95002"],
                                  "900022": ["92302"]}
        assert [item["album_id"] for item in new["items"]] == ["900011", "900020", "900022", "900500"]  # oldest first
        assert new["counts"] == {"albums": 4, "chapters": 5} and new["more"] == 0
        assert _reasons(new) == [("900021", "active")]
        assert [(r["album_id"], r["new_count"], r["removed_count"]) for r in new["review"]] == [("900009", 1, 1)]
        assert new["out_of_scope"] == {"changed": 1}
        assert [c["photo_id"] for c in new["items"][1]["chapters"]] == ["92103", "92104"]
        # 900700 (files deleted, a stale 'new' row) and 900040 (never checked) appear nowhere
        listed = {i["album_id"] for i in new["items"]} | {s["album_id"] for s in new["skipped"]}
        assert not listed & {"900700", "900040"}

        # 下载未下载的收藏: 900701, 900008 (never), 900030 (canceled); 900032 skipped; the outside counts
        undownloaded = _preview(client, "undownloaded_favourites")
        assert [(i["album_id"], i["state"]) for i in undownloaded["items"]] == [   # newest favourite first
            ("900030", "canceled"), ("900008", "never"), ("900701", "never")]
        assert all(item["scope"] == "all" and item["photo_ids"] == [] for item in undownloaded["items"])
        assert _reasons(undownloaded) == [("900032", "records_cleared")]
        # readable 900002-900004, 900009-900011; missing 900005-900007 (corrupt, no pages, deleted) and 900700
        assert undownloaded["out_of_scope"] == {"readable": 6, "active": 1, "failed": 1, "missing": 4}

        # 下载选中的收藏: only the 未下载 rows of a selection, the rest with their reasons (in selection order)
        selection = ["900701", "900030", "900031", "900007", "900011", "900033", "900032", "900500"]
        selected = _preview(client, "selected_favourites", album_ids=selection)
        assert [item["album_id"] for item in selected["items"]] == ["900701", "900030"]
        assert _reasons(selected) == [("900031", "failed"), ("900007", "missing"), ("900011", "readable"),
                                      ("900033", "active"), ("900032", "records_cleared"),
                                      ("900500", "not_favourite")]
        assert metrics["schedule_calls"] == 0 and len(db.get_all_jobs()) == len(seeded)

        # confirm 下载新章节 with 900500 unticked (the click script): three queued jobs, one schedule call, no download
        response = client.post(CONFIRM, json={"kind": "new_chapters", "token": new["token"], "exclude": ["900500"]})
        assert response.status_code == 201, response.get_json()
        assert [(j["album_id"], j["photo_ids"]) for j in response.get_json()["created"]] == [
            ("900011", ["91104"]), ("900020", ["92103", "92104"]), ("900022", ["92302"])]
        assert metrics["schedule_calls"] == 1 and metrics["download_calls"] == 0 and not _dl_threads()
        view = json.loads(json.dumps(fixture.batch_metrics(db, manager, metrics, seeded)))   # the route's JSON
        assert view["schedule"] == "stub" and view["download"] == "refuse" and view["dl_threads"] == []
        assert (view["schedule_calls"], view["download_calls"]) == (1, 0)
        assert _new_jobs(view) == [("900011", "queued", ["91104"]), ("900020", "queued", ["92103", "92104"]),
                                   ("900022", "queued", ["92302"])]
        assert len(view["jobs"]) == len(seeded) + 3

        # confirm 下载未下载的收藏: three whole-comic jobs
        response = client.post(CONFIRM, json={"kind": "undownloaded_favourites", "token": undownloaded["token"]})
        assert response.status_code == 201, response.get_json()
        assert [j["album_id"] for j in response.get_json()["created"]] == ["900030", "900008", "900701"]
        assert metrics["schedule_calls"] == 2

        # 收藏 row 下载: a queued comic gets no second job (and no schedule call); another one is recorded
        response = client.post("/api/wishlist/download", json={"ids": ["900701"]})
        assert response.status_code == 200 and response.get_json()["skipped"] == [
            {"album_id": "900701", "reason": "active"}]
        response = client.post("/api/wishlist/download", json={"ids": ["900031"]})
        assert response.status_code == 201 and metrics["schedule_calls"] == 3
        # the detail page's 下载选中章节 (POST /api/jobs) and 下载管理 重试 only record too
        response = client.post("/api/jobs", json={"album_id": "900040", "photo_ids": ["94101"], "title": "Refresh"})
        assert response.status_code == 201 and metrics["schedule_calls"] == 4
        response = client.post("/api/jobs/job_ui_failed/retry")
        assert response.status_code == 201

        view = fixture.batch_metrics(db, manager, metrics, seeded)
        assert [(album_id, status) for album_id, status, _ in _new_jobs(view)] == [
            ("900011", "queued"), ("900020", "queued"), ("900022", "queued"), ("900030", "queued"),
            ("900008", "queued"), ("900701", "queued"), ("900031", "queued"), ("900040", "queued"), ("900600", "queued")]
        assert view["download_calls"] == 0 and view["dl_threads"] == [] and not _dl_threads()
        manager.start()                                   # a no-op: the 2 s loop never runs in the fixture
        assert manager._scheduler_thread is None
        # anything that still reached the download function is refused (and counted)
        with pytest.raises(RuntimeError, match="the UI fixture never downloads"):
            jm_service.download_album_job("job_x", "900701", [])
        assert metrics["download_calls"] == 1
        assert all(job["status"] != "running" for job in db.get_all_jobs())
    view = fixture.batch_metrics(db, job_module.job_manager, metrics, seeded)
    assert view["schedule"] == "REAL" and view["download"] == "REAL"     # the real ones are back after the test


def test_ui_fixture_refresh_sample_gains_its_chapter_only_after_the_check(client, downloads):
    from PIL import Image, ImageDraw
    from core import database as db, update_checker
    fixture = _fixture_module()
    fixture.add_batch_samples(downloads, db, Image, ImageDraw)
    _forget_local()
    before = fixture.refresh_detail([])
    assert [p["photo_id"] for p in before["photos"]] == ["94101", "94102"] and before["chapter_count"] == 2
    db.set_cached_album_detail("900040", json.dumps(before, ensure_ascii=False))   # opening the detail page
    state = client.get("/api/updates/900040").get_json()
    assert state["eligible"] is True and state["update"]["state"] == "never"
    assert "900040" not in _chapters(_preview(client, "new_chapters"))
    metrics = fixture.install_update_stub(update_checker)
    with _job_stub(fixture) as jobs:
        count = len(db.get_all_jobs())
        check = client.post("/api/updates/900040/check").get_json()   # 立即检查
        assert check["outcome"] == "new" and check["update"]["state"] == "new"
        assert [c["photo_id"] for c in check["update"]["new_chapters"]] == ["94103"]
        assert metrics["calls"] == ["900040"]
        after = fixture.refresh_detail(metrics["calls"])
        assert [p["photo_id"] for p in after["photos"]] == ["94101", "94102", "94103"] and after["chapter_count"] == 3
        db.set_cached_album_detail("900040", json.dumps(after, ensure_ascii=False))   # the table refresh's GET
        assert client.get("/api/local-chapters/900040").get_json()["partial"] == {"downloaded": 2, "total": 3}
        # now a 下载新章节 target; the check itself created and scheduled nothing
        assert _chapters(_preview(client, "new_chapters"))["900040"] == ["94103"]
        assert len(db.get_all_jobs()) == count and jobs["schedule_calls"] == 0 and jobs["download_calls"] == 0


def test_ui_fixture_many_favourites_show_the_cap(client, downloads):
    from core import database as db
    fixture = _fixture_module()
    fixture.add_many_favourites(db)
    data = _preview(client, "undownloaded_favourites")
    assert len(data["items"]) == 50 and data["more"] == 10 and data["limit"] == 50
    assert {item["album_id"] for item in data["items"]} <= {str(901000 + n) for n in range(60)}


def test_ui_fixture_guards_job_stub_before_serving():
    source = FIXTURE_PATH.read_text(encoding="utf-8")
    main = source[source.index("def main():"):]
    seed = main.index("seed_samples(")
    update_stub = main.index("install_update_stub(update_checker)")
    stub = main.index("install_job_stub(job_module.job_manager)")
    scheduled = main.index('assert job_module.job_manager._schedule_next is job_metrics["stub"]')
    refused = main.index('assert job_module.download_album_job is job_metrics["refuse"]')
    idle = main.index("assert job_module.job_manager._scheduler_thread is None")
    serve = main.index("serve(app")
    assert seed < update_stub < stub < scheduled < refused < idle < serve
    assert main.count("install_job_stub(") == 1
    # nothing in the fixture starts the download loop or the 定时下载 thread
    assert main.count(".start()") == 1 and "update_checker.start()" in main   # only the --fast-updates check loop
    assert "start_scheduler" not in main and "sync_with_settings" not in main
    assert '"/test/batch-metrics"' in main
    assert "batch_metrics(db, job_module.job_manager, job_metrics, seeded_jobs)" in main
    assert 'refresh_detail(update_metrics["calls"])' in main
    assert '"--many-favourites"' in main and "many_favourites=args.many_favourites" in main
