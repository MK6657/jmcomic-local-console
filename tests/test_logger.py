"""Logging core: template dedup, summaries, rotation, retention, library logger takeover.

core.logger is only imported inside fixtures/tests (never at collection time) and always bound to a
temporary app root, so nothing here can touch the real runtime/logs.
"""
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] \[(?P<level>\w+)\] \[(?P<name>[^\]]+)\] (?P<msg>.*)$")
SUMMARY_RE = re.compile(r"^↑ 同类消息 (?P<count>\d+) 次已合并（\d\d:\d\d:\d\d–\d\d:\d\d:\d\d），最后一条：(?P<last>.*)$")


def _letters(k=12):
    return "".join(random.choices("ghijklmnopqrstuvwxyz", k=k))


@pytest.fixture(scope="module")
def lm(tmp_path_factory):
    """core.logger bound to a temporary app root (or to whichever temp root imported it first)."""
    import core.path_utils as paths
    with pytest.MonkeyPatch.context() as patch:
        if "core.logger" not in sys.modules:
            root = tmp_path_factory.mktemp("logger-root")
            patch.setattr(paths, "get_app_root", lambda: root)
        import core.logger as module
    if Path(module.LOG_DIR).resolve() == (REPO_ROOT / "runtime" / "logs").resolve():
        pytest.skip("core.logger was imported with the real runtime directory; refusing to write there")
    return module


class FakeClock:
    def __init__(self, start=None):
        self.now = time.time() if start is None else start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Pipeline:
    """An isolated copy of the production wiring: one shared dedup gate on app.log + error.log handlers."""

    def __init__(self, lm, directory, windows=None, max_entries=None, adopted=None):
        self.clock = FakeClock()
        kwargs = {"windows": windows, "clock": self.clock, "adopted": adopted}
        if max_entries is not None:
            kwargs["max_entries"] = max_entries
        self.dedup = lm.LogDeduplicator(**kwargs)
        self.app_file = directory / "app.log"
        self.error_file = directory / "error.log"
        formatter = lm.StructuredFormatter(lm.LOG_FORMAT, datefmt=lm.DATE_FORMAT)
        self.handlers = []
        for path, level in ((self.app_file, logging.INFO), (self.error_file, logging.WARNING)):
            handler = lm.DailyRotatingFileHandler(path)
            handler.setLevel(level)
            handler.setFormatter(formatter)
            handler.addFilter(self.dedup)
            self.handlers.append(handler)
        self.logger = self.attach(f"testlog.{_letters()}")
        self.log = lm.LogContext(self.logger)

    def attach(self, name):
        logger = logging.getLogger(name)
        logger.handlers[:] = list(self.handlers)
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        return logger

    def records(self, which="app"):
        path = self.app_file if which == "app" else self.error_file
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            match = LINE_RE.match(line)
            if match:
                message = re.sub(r' \{".*\}$', "", match["msg"])
                result.append((match["level"], match["name"], message))
        return result

    def messages(self, which="app"):
        return [message for _, _, message in self.records(which)]

    def close(self):
        for handler in self.handlers:
            handler.close()


@pytest.fixture
def pipe(lm, tmp_path):
    pipeline = Pipeline(lm, tmp_path)
    yield pipeline
    pipeline.close()


def _burst(pipe, level="warning", pages=range(10, 18)):
    for page in pages:
        getattr(pipe.log, level)(
            f"在线阅读 取图 photo_id=324751 page={page} 超时或失败 error=unknown file extension: .part")
        pipe.clock.advance(0.3)


# ── templates ─────────────────────────────────────────────

def test_template_normalizes_numbers_and_ids(lm):
    t = lm.message_template
    assert t("取图 photo_id=324751 page=10 失败") == t("取图 photo_id=324751 page=17 失败")
    assert t("HTTP GET /api/online-img/324751/9 → 200 OK (80ms)") == t("HTTP GET /api/online-img/1/10 → 200 OK (7ms)")
    assert t("request req_ab12cd34 done") == t("request req_zz99yy88 done")
    assert t("hash 3fa2b9c1d4e5 bad") == t("hash 00ff00ff00ff bad")
    assert t("id 123e4567-e89b-12d3-a456-426614174000") == t("id 00000000-0000-0000-0000-00000000000a")
    assert t("下载失败 album=1") != t("下载完成 album=1")
    assert t("HTTP GET /x → 200 OK (1ms)") != t("HTTP GET /x → 404 NOT FOUND (1ms)")
    # identity fields keep their values: different jobs/albums/photos are different templates
    assert t("任务完成 job_id=job_3fa2b9c1d4e5 album_id=350001") != t("任务完成 job_id=job_00ff00ff00ff album_id=350001")
    assert t("创建任务 album_id=350001 job_id=11") != t("创建任务 album_id=350002 job_id=11")
    assert t("取图 photo_id=324751 page=1") != t("取图 photo_id=324752 page=1")
    assert t("x album_id=350001, page=1") == t("x album_id=350001, page=2")
    assert t("失败 request_id=req_ab12cd34 code=1") == t("失败 request_id=req_zz99yy88 code=2")  # not an identity


def test_messages_about_different_jobs_are_never_merged(pipe):
    jobs = [f"job_{n:012x}" for n in (11, 12, 13, 14)]
    for n, job in enumerate(jobs):
        pipe.log.error(f"下载任务异常 job_id={job} album_id=35000{n} error=请求重试全部失败")
    pipe.log.error(f"下载任务异常 job_id={jobs[0]} album_id=350000 error=请求重试全部失败")  # same job again
    pipe.dedup.flush(force=True)
    messages = pipe.messages("error")
    for n, job in enumerate(jobs):  # every job and album can still be found in the log
        assert f"下载任务异常 job_id={job} album_id=35000{n} error=请求重试全部失败" in messages
    summaries = [m for m in messages if SUMMARY_RE.match(m)]
    assert len(messages) == 5 and len(summaries) == 1 and jobs[0] in summaries[0]


def test_every_job_whose_selection_cannot_be_parsed_is_named_in_the_log(client):
    """The scheduler's warning wrote the job as "Job job_<hex>": not an identity field, so the warnings of
    different jobs were merged and the middle job's failure was written nowhere."""
    from core import database as db, logger as lm
    from core.job_manager import JobManager
    manager = JobManager()
    jobs = [manager.create_job(album, f"t{album}", ["1"]) for album in ("111", "222", "333")]
    conn = db.get_db()
    try:
        conn.execute("UPDATE jobs SET selected_photo_ids='not json'")
        conn.commit()
    finally:
        conn.close()
    for _ in jobs:
        manager._schedule_next()  # the production path that logs the warning and marks the job failed
    lm.flush_log_summaries(force=True)
    text = lm.ERROR_LOG_FILE.read_text(encoding="utf-8")
    for job_id in jobs:
        assert db.get_job(job_id)["status"] == "failed"
        assert f"selected_photo_ids 解析失败，标记为 failed job_id={job_id} " in text


def test_different_exceptions_with_the_same_message_keep_their_tracebacks(pipe):
    def fail(exc):
        try:
            raise exc
        except Exception:
            pipe.logger.error("Exception on /api/online-img/324751/1 [GET]", exc_info=True)
    fail(ValueError("first failure"))
    fail(PermissionError(13, "second failure"))
    fail(ValueError("first failure again"))  # same exception type and place: merged
    pipe.dedup.flush(force=True)
    text = pipe.error_file.read_text(encoding="utf-8")
    assert "ValueError: first failure" in text and "PermissionError: [Errno 13] second failure" in text
    assert "first failure again" not in text.split("↑ 同类消息")[0]
    assert text.count("Traceback (most recent call last)") == 2
    assert len([m for m in pipe.messages("error") if SUMMARY_RE.match(m)]) == 1


# ── dedup ─────────────────────────────────────────────────

def test_burst_is_first_line_plus_one_summary_with_count(pipe):
    _burst(pipe)
    assert pipe.messages() == [
        "在线阅读 取图 photo_id=324751 page=10 超时或失败 error=unknown file extension: .part"]
    pipe.clock.advance(61)
    assert pipe.dedup.flush() == 1
    records = pipe.records()
    assert len(records) == 2
    level, name, message = records[1]
    assert (level, name) == ("WARNING", pipe.logger.name)
    summary = SUMMARY_RE.match(message)
    assert summary, message
    assert summary["count"] == "7"
    assert summary["last"].startswith("在线阅读 取图 photo_id=324751 page=17 ")
    # one shared state: error.log saw exactly the same first line + summary
    assert pipe.records("error") == records
    assert pipe.dedup.flush(force=True) == 0


def test_summary_is_never_attached_to_an_unrelated_record(pipe):
    for page in range(5):
        pipe.log.error(f"取图失败 page={page}")
    pipe.clock.advance(31)  # window over, but nothing flushed yet
    pipe.log.info("API同步标签完成 album_id=324751 tags=3")
    pipe.log.error("另一个错误 code=7")
    assert pipe.messages() == ["取图失败 page=0", "API同步标签完成 album_id=324751 tags=3", "另一个错误 code=7"]
    pipe.dedup.flush()
    messages = pipe.messages()
    assert messages[:3] == ["取图失败 page=0", "API同步标签完成 album_id=324751 tags=3", "另一个错误 code=7"]
    assert len(messages) == 4 and SUMMARY_RE.match(messages[3])["count"] == "4"
    assert SUMMARY_RE.match(messages[3])["last"] == "取图失败 page=4"
    assert sum("同类消息" in m or "重复" in m for m in messages) == 1


def test_summary_is_flushed_when_template_recurs_after_window(pipe):
    for n in range(1, 5):
        pipe.log.info(f"HTTP GET /api/album/{n} → 200 OK ({n}ms)")
    pipe.clock.advance(121)
    pipe.log.info("HTTP GET /api/album/9 → 200 OK (9ms)")
    messages = pipe.messages()
    assert messages[0] == "HTTP GET /api/album/1 → 200 OK (1ms)"
    assert SUMMARY_RE.match(messages[1])["count"] == "3"
    assert SUMMARY_RE.match(messages[1])["last"] == "HTTP GET /api/album/4 → 200 OK (4ms)"
    assert messages[2] == "HTTP GET /api/album/9 → 200 OK (9ms)"  # starts a new window, logged in full
    assert len(messages) == 3


def test_periodic_flush_writes_summary_during_quiet_period(lm, pipe):
    keeper = lm.LogHousekeeper(pipe.dedup, interval=0.02)
    keeper.ensure_started()
    try:
        _burst(pipe, pages=range(3))
        pipe.clock.advance(61)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(pipe.messages()) < 2:
            time.sleep(0.02)
    finally:
        keeper.stop(flush=False)
    messages = pipe.messages()
    assert len(messages) == 2 and SUMMARY_RE.match(messages[1])["count"] == "2"


def test_shutdown_flushes_pending_summaries(lm, pipe):
    keeper = lm.LogHousekeeper(pipe.dedup, interval=3600)
    keeper.ensure_started()
    _burst(pipe, pages=range(4))  # window still open
    keeper.stop(flush=True)
    messages = pipe.messages()
    assert len(messages) == 2 and SUMMARY_RE.match(messages[1])["count"] == "3"
    assert not keeper._thread.is_alive()


def test_forced_flush_keeps_windows_of_messages_without_repeats(pipe):
    """flush(force=True) (the launcher's flush endpoint) writes the pending counts; a message seen once must
    keep its window, or every recently logged message is written in full again after each call."""
    pipe.log.warning("取图失败 photo_id=1 page=1")
    pipe.log.warning("封面加载慢 n=1")
    pipe.log.warning("封面加载慢 n=2")
    assert pipe.dedup.flush(force=True) == 1  # only the template that had a repeat
    pipe.clock.advance(1)
    pipe.log.warning("取图失败 photo_id=1 page=2")  # still inside its window: merged, not logged in full
    pipe.log.warning("取图失败 photo_id=1 page=3")
    pipe.log.warning("封面加载慢 n=3")  # its window ended with the summary: a new round starts in full
    assert pipe.dedup.flush(force=True) == 1
    messages = pipe.messages("error")
    assert messages[0] == "取图失败 photo_id=1 page=1" and messages[1] == "封面加载慢 n=1"
    assert SUMMARY_RE.match(messages[2])["last"] == "封面加载慢 n=2"
    assert messages[3] == "封面加载慢 n=3"
    assert SUMMARY_RE.match(messages[4])["count"] == "2"
    assert SUMMARY_RE.match(messages[4])["last"] == "取图失败 photo_id=1 page=3"
    assert len(messages) == 5
    pipe.clock.advance(61)
    assert pipe.dedup.flush() == 0 and pipe.dedup.pending_count() == 0


def test_levels_and_loggers_are_never_merged(pipe):
    other = pipe.attach(f"testlog.{_letters()}")
    for n in (1, 2):
        pipe.log.info(f"同一条消息 id={n}")
        pipe.log.warning(f"同一条消息 id={n}")
        other.info(f"同一条消息 id={n}")
    records = pipe.records()
    assert records == [
        ("INFO", pipe.logger.name, "同一条消息 id=1"),
        ("WARNING", pipe.logger.name, "同一条消息 id=1"),
        ("INFO", other.name, "同一条消息 id=1"),
    ]
    pipe.dedup.flush(force=True)
    summaries = [(level, name) for level, name, message in pipe.records()[3:] if SUMMARY_RE.match(message)]
    assert sorted(summaries) == sorted([("INFO", pipe.logger.name), ("WARNING", pipe.logger.name),
                                        ("INFO", other.name)])


def test_evicted_template_emits_its_summary_first(lm, tmp_path):
    pipe = Pipeline(lm, tmp_path, max_entries=2)
    try:
        pipe.log.info("A n=1")
        pipe.log.info("A n=2")
        pipe.log.info("B")
        pipe.log.info("C")  # evicts A (least recently used)
        messages = pipe.messages()
        assert messages[0] == "A n=1" and messages[1] == "B"
        assert SUMMARY_RE.match(messages[2])["last"] == "A n=2"
        assert messages[3] == "C"
    finally:
        pipe.close()


def test_concurrent_logging_counts_every_repeat(pipe):
    def worker(offset):
        for n in range(50):
            pipe.log.warning(f"并发失败 item={offset + n}")
    threads = [threading.Thread(target=worker, args=(i * 100,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    pipe.dedup.flush(force=True)
    messages = pipe.messages()
    assert len(messages) == 2 and SUMMARY_RE.match(messages[1])["count"] == "199"


def test_third_party_records_are_sanitized_and_info_is_dropped(lm, tmp_path):
    pipe = Pipeline(lm, tmp_path, adopted={"ours"})
    try:
        external = pipe.attach(f"thirdparty.{_letters()}")
        external.info("routine chatter")
        external.warning("login failed cookie=abc123 token: xyz")
        try:
            raise ValueError("password=hunter2")
        except ValueError:
            external.error("boom", exc_info=True)
        text = pipe.app_file.read_text(encoding="utf-8")
        assert "routine chatter" not in text
        assert "cookie=COOKIE" in text and "token: TOKEN" in text and "abc123" not in text
        assert "hunter2" not in text and "password=****" in text
        assert "Traceback (most recent call last)" in text
    finally:
        pipe.close()


# ── line format ───────────────────────────────────────────

def test_line_breaks_cannot_forge_log_entries(lm, pipe):
    forged = "\n[2026-09-25 03:00:00] [ERROR] [jmconsole] forged-entry"
    pipe.log.info(f"HTTP GET /x{forged} → 403 FORBIDDEN (1ms)")
    pipe.log.warning(f"库输出\r[2026-09-25 03:00:01] [ERROR] [jmcomic] forged-cr")
    try:
        raise ValueError("body:\r\n[2026-09-25 03:00:02] [CRITICAL] [jmconsole] forged-in-traceback")
    except ValueError:
        pipe.log.exception("解析失败")
    raw = pipe.app_file.read_bytes()
    assert b"\r" not in raw.replace(b"\r\n", b"")  # no lone CR that an editor would show as a new line
    lines = raw.decode("utf-8").splitlines()
    headers = [line for line in lines if lm.LOG_HEADER_RE.match(line)]
    assert len(headers) == 3 and not any(line.startswith("[2026-09-25 03:00:0") for line in lines)
    assert "HTTP GET /x\\n[2026-09-25 03:00:00] [ERROR] [jmconsole] forged-entry → 403 FORBIDDEN (1ms)" in headers[0]
    assert "库输出\\r[2026-09-25 03:00:01]" in headers[1]
    assert " [2026-09-25 03:00:02] [CRITICAL] [jmconsole] forged-in-traceback" in lines  # indented continuation


def test_formatted_lines_match_the_viewer_header(lm, pipe):
    pipe.log.warning("封面加载慢 album_id=1", extra={"album_id": "1"})
    line = pipe.error_file.read_text(encoding="utf-8").splitlines()[0]
    match = lm.LOG_HEADER_RE.match(line)
    assert match and match.group(2) == "WARNING" and match.group(3) == pipe.logger.name
    assert match.group(4).startswith("封面加载慢 album_id=1 {")


# ── request ids ───────────────────────────────────────────

def _payloads(path):
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LINE_RE.match(line)
        if match:
            message, _, payload = match["msg"].partition(" {")
            result[message] = json.loads("{" + payload) if payload else {}
    return result


def test_only_request_or_task_records_carry_a_request_id(lm, pipe):
    external = pipe.attach(f"thirdparty.{_letters()}")

    def thread_body():
        pipe.log.warning("后台线程 无请求")
        rid = lm.set_request_id()
        pipe.log.warning("任务线程 已设置")
        worker = threading.Thread(target=lm.bind_request_id(lambda: (
            pipe.log.warning("任务工作线程"), external.warning("库重试 工作线程"))))
        worker.start()
        worker.join()
        lm.clear_request_id()
        pipe.log.warning("任务结束后")
        results["rid"] = rid

    results = {}
    thread = threading.Thread(target=thread_body)
    thread.start()
    thread.join()
    payloads = _payloads(pipe.error_file)
    rid = results["rid"]
    assert "request_id" not in payloads["后台线程 无请求"]
    assert payloads["任务线程 已设置"]["request_id"] == rid
    assert payloads["任务工作线程"]["request_id"] == rid
    assert payloads["库重试 工作线程"]["request_id"] == rid  # third-party records join the trace too
    assert "request_id" not in payloads["任务结束后"]


def _summary_payloads(path):
    """(last message, JSON payload) of every dedup summary line, in file order."""
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LINE_RE.match(line)
        if match and match["msg"].startswith("↑ 同类消息"):
            message, _, payload = match["msg"].partition(" {")
            result.append((SUMMARY_RE.match(message)["last"], json.loads("{" + payload)))
    return result


def test_summary_lists_every_request_it_merged(lm, pipe, monkeypatch):
    """A summary covering several requests must not be filed under the last one: that request's trace would
    show the other requests' records and theirs would miss them. One request keeps its single request_id."""
    def as_request(rid, message):
        if rid:
            lm.set_request_id(rid)
        try:
            pipe.log.warning(message)
        finally:
            lm.clear_request_id()
    for rid in ("req_aaaaaaa1", "req_bbbbbbb2", "req_ccccccc3"):
        as_request(rid, f"【req.retry】重试第1次 url=https://cdn.example.test/album/{rid[-1]}")
    for n in range(3):
        as_request("req_ddddddd4", f"下载图片失败 page={n}")      # one job/request, repeated
    as_request(None, "封面缓存写入失败 n=1")
    as_request("req_eeeeeee5", "封面缓存写入失败 n=2")
    as_request(None, "封面缓存写入失败 n=3")                    # mixed with records outside any request
    monkeypatch.setattr(lm, "SUMMARY_MAX_REQUEST_IDS", 3)
    for n in range(6):
        as_request(f"req_fffffff{n}", f"取图超时 n={n}")
    pipe.dedup.flush(force=True)
    summaries = dict(_summary_payloads(pipe.error_file))
    assert summaries["【req.retry】重试第1次 url=https://cdn.example.test/album/3"] == {
        "merged": 2, "request_ids": ["req_bbbbbbb2", "req_ccccccc3"]}
    assert summaries["下载图片失败 page=2"] == {"merged": 2, "request_id": "req_ddddddd4"}
    assert summaries["封面缓存写入失败 n=3"] == {"merged": 2, "request_ids": ["req_eeeeeee5"]}
    assert summaries["取图超时 n=5"] == {"merged": 5, "request_ids": ["req_fffffff3", "req_fffffff4", "req_fffffff5"],
                                     "request_ids_omitted": 2}


def test_only_this_request_finds_summaries_that_merged_it(client):
    """End to end: three requests hit the same library retry warning; "只看此请求" for the middle one must
    show the summary that absorbed its record, and the summary must not claim to be the last request."""
    from core import logger as lm
    marker = _letters()
    rids = ["req_trace0a1", "req_trace0b2", "req_trace0c3"]
    for n, rid in enumerate(rids):
        lm.set_request_id(rid)
        try:
            logging.getLogger("jmcomic").warning(f"【req.retry】{marker} url=https://cdn.example.test/album/1111{n}")
        finally:
            lm.clear_request_id()
    lm.flush_log_summaries(force=True)

    def trace(rid):
        data = client.get("/api/system/logs", query_string={"source": "app", "q": rid}).get_json()
        return [entry for entry in data["entries"] if marker in entry["message"]]
    first, middle, last = (trace(rid) for rid in rids)
    assert [e["request_id"] for e in first] == [rids[0]] and "同类消息" not in first[0]["message"]
    assert len(middle) == 1 and middle[0]["message"].startswith("↑ 同类消息 2 次已合并")
    assert middle[0]["request_id"] == "" and middle[0]["extra"] == {"merged": 2, "request_ids": rids[1:]}
    assert last == middle


def test_request_context_id_is_shared_with_the_thread(lm):
    from flask import Flask
    with Flask("rid-test").test_request_context("/"):
        rid = lm.set_request_id()
        assert lm.current_request_id() == rid
    with Flask("rid-test").test_request_context("/"):
        lm.clear_request_id()
        generated = lm.current_request_id()
        assert generated and lm.current_request_id() == generated
    lm.clear_request_id()
    assert lm.current_request_id() is None


def test_download_job_thread_has_its_own_request_id(client, monkeypatch):
    from core import job_manager as jm_module, logger as lm
    seen = []

    def fake_job(job_id, album_id, photo_ids):
        seen.append(lm.current_request_id())
        worker = threading.Thread(target=lm.bind_request_id(lambda: seen.append(lm.current_request_id())))
        worker.start()
        worker.join()
    monkeypatch.setattr(jm_module, "download_album_job", fake_job)

    def run():
        jm_module.job_manager._run_job_wrapper("job_rid000test", "1", [])
        seen.append(lm.current_request_id())
    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert seen[0] and seen[0].startswith("req_") and seen[1] == seen[0]
    assert seen[2] is None  # cleared when the job ends


def _newest_entry(client, query):
    data = client.get("/api/system/logs", query_string={"source": "app", "q": query, "limit": 1}).get_json()
    return data["entries"][0]


def test_tag_sync_upstream_failure_joins_the_request_trace(client, monkeypatch):
    """The upstream call runs in a pool thread; without bind_request_id the cause of a failed sync (and the
    library's retry records) had no request_id, so "只看此请求" showed the 504 but not why."""
    from core import jm_service
    album = str(random.randint(10**8, 10**9))

    class FailingClient:
        def get_album_detail(self, album_id):
            logging.getLogger("jmcomic").info(f"请求失败 url=https://cdn.example.test/album/{album_id}",
                                              extra={"topic": "req.retry"})
            raise ConnectionError(f"upstream refused album {album_id}")
    monkeypatch.setattr(jm_service, "get_client", lambda shared=True: (FailingClient(), None))
    jm_service.clear_album_detail_cache()
    assert client.post(f"/api/library/{album}/tags/sync").status_code == 504
    data = client.get("/api/system/logs", query_string={"source": "app", "q": album, "limit": 50}).get_json()
    messages = {entry["message"].split("\n")[0]: entry["request_id"] for entry in data["entries"]}
    rid = messages[f"API同步标签超时 album_id={album}"]
    assert rid.startswith("req_")
    cause = f"获取专辑详情 album_id={album} 超时或失败 error=ConnectionError: upstream refused album {album}"
    assert messages[cause] == rid
    assert messages[f"【req.retry】请求失败 url=https://cdn.example.test/album/{album}"] == rid
    assert set(messages.values()) == {rid}


@pytest.mark.parametrize("route", ["wishlist", "sync-all"])
def test_background_tag_sync_threads_keep_the_starting_request_id(client, monkeypatch, route):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from core import logger as lm
    from routes import api_library, api_wishlist
    started, seen = [], []

    def thread(target, args=(), **kwargs):  # only these two routes' threads; logging's own stay real
        started.append((target, args))
        return Mock()
    for module in (api_library, api_wishlist):
        monkeypatch.setattr(module, "threading", SimpleNamespace(Thread=thread))

    def detail(album_id):
        seen.append(lm.current_request_id())
        return {"tags": []}
    monkeypatch.setattr(api_wishlist, "get_album_detail_cached", detail)
    monkeypatch.setattr(api_library, "get_album_detail_cached", detail)
    album = str(random.randint(10**8, 10**9))
    if route == "wishlist":
        response = client.post("/api/wishlist", json={"album_id": album, "title": "t", "author": "a"})
        query = f"添加收藏 album_id={album}"
    else:
        monkeypatch.setattr(api_library.db, "get_library", lambda **kwargs: {"items": [{"album_id": album}]})
        response = client.post("/api/library/tags/sync-all")
        query = "API批量同步标签已接受"
    assert len(started) == 1
    target, args = started[0]
    target(*args)  # the thread body, run after the request has ended (also releases the bulk-sync lock)
    assert response.status_code in (201, 202)
    rid = _newest_entry(client, query)["request_id"]
    assert rid.startswith("req_") and seen == [rid]
    assert lm.current_request_id() is None  # the id does not leak into the thread that ran it


def test_sse_stream_failure_is_logged_with_the_request_id(client, monkeypatch):
    """An exception inside the event stream used to leave stream_with_context first (popping the request
    context), so the only record of it was waitress's "Exception while serving" without a request_id."""
    from core.progress import progress_manager
    job_id = "job_" + "".join(random.choices("0123456789abcdef", k=12))
    tracker = progress_manager.create_tracker(job_id)

    def failing_events(client_queue=None):
        yield f"event: progress\ndata: {json.dumps({'job_id': job_id})}\n\n"
        json.dumps({"job_id": job_id, "bad": object()})  # an event that cannot be encoded
    monkeypatch.setattr(tracker, "iter_events", failing_events)
    try:
        response = client.get(f"/api/jobs/{job_id}/events")
        body = response.get_data(as_text=True)  # the stream ends instead of raising into the server
    finally:
        progress_manager.remove_tracker(job_id)
    assert response.status_code == 200 and body.startswith("event: progress")
    data = client.get("/api/system/logs", query_string={"source": "app", "q": job_id, "limit": 50}).get_json()
    by_message = {entry["message"].split("\n")[0]: entry for entry in data["entries"]}
    rid = by_message[f"API SSE连接 job_id={job_id}"]["request_id"]
    failure = by_message[f"SSE 推送异常，已结束本条事件流 job_id={job_id}"]
    assert rid.startswith("req_") and failure["request_id"] == rid and failure["level"] == "ERROR"
    assert "TypeError: Object of type object is not JSON serializable" in failure["message"]
    assert tracker.subscriber_count() == 0


# ── rotation ──────────────────────────────────────────────

def _file_logger(lm, handler):
    handler.setFormatter(lm.StructuredFormatter(lm.LOG_FORMAT, datefmt=lm.DATE_FORMAT))
    logger = logging.getLogger(f"testrot.{_letters()}")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


def test_handler_rotates_at_midnight_named_by_content_day(lm, tmp_path):
    clock = FakeClock(time.mktime((2026, 9, 20, 23, 59, 0, 0, 0, -1)))
    handler = lm.DailyRotatingFileHandler(tmp_path / "app.log", clock=clock)
    try:
        logger = _file_logger(lm, handler)
        logger.info("day one")
        clock.advance(120)
        logger.info("day two")
    finally:
        handler.close()
    assert "day one" in (tmp_path / "app.log.2026-09-20").read_text(encoding="utf-8")
    active = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "day two" in active and "day one" not in active


def test_handler_rotates_stale_file_after_restart(lm, tmp_path):
    path = tmp_path / "error.log"
    path.write_text("[2026-09-01 10:00:00] [ERROR] [jmconsole] old\n", encoding="utf-8")
    stale = time.time() - 2 * 86400
    os.utime(path, (stale, stale))
    handler = lm.DailyRotatingFileHandler(path)
    try:
        _file_logger(lm, handler).warning("fresh")
    finally:
        handler.close()
    day = time.strftime("%Y-%m-%d", time.localtime(stale))
    assert "old" in (tmp_path / f"error.log.{day}").read_text(encoding="utf-8")
    assert "old" not in path.read_text(encoding="utf-8")


def test_handler_rotates_early_when_active_file_is_too_large(lm, tmp_path):
    handler = lm.DailyRotatingFileHandler(tmp_path / "app.log", max_bytes=300)
    try:
        logger = _file_logger(lm, handler)
        for n in range(20):
            logger.info(f"line {n:02d} " + "x" * 40)
    finally:
        handler.close()
    files = sorted(tmp_path.glob("app.log*"))
    assert len(files) >= 3
    assert (tmp_path / "app.log").stat().st_size < 400
    lines = [line for f in files for line in f.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 20  # nothing lost, nothing overwritten


def test_rotation_target_never_reuses_a_name_freed_by_retention(lm, tmp_path):
    base = str(tmp_path / "app.log")
    assert lm.rotation_target(base, "2026-09-24") == f"{base}.2026-09-24"
    (tmp_path / "app.log.2026-09-24").write_text("x", encoding="utf-8")
    assert lm.rotation_target(base, "2026-09-24") == f"{base}.2026-09-24.1"
    for name in ("app.log.2026-09-24.2", "app.log.2026-09-24.3", "app.log.2026-09-23.9", "app.log.2026-09-24.x"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "app.log.2026-09-24").unlink()  # the size cap deleted the day's oldest archive
    assert lm.rotation_target(base, "2026-09-24") == f"{base}.2026-09-24.4"  # newer than .3, not the freed base
    assert lm.rotation_target(base, "2026-09-25") == f"{base}.2026-09-25"


def test_handler_keeps_logging_when_rotation_is_blocked(lm, tmp_path, monkeypatch):
    clock = FakeClock(time.mktime((2026, 9, 20, 23, 59, 0, 0, 0, -1)))
    handler = lm.DailyRotatingFileHandler(tmp_path / "app.log", clock=clock)
    try:
        logger = _file_logger(lm, handler)
        logger.info("before midnight")
        clock.advance(120)

        def locked(*args, **kwargs):
            raise PermissionError("file is used by another process")
        with monkeypatch.context() as patch:
            patch.setattr(os, "rename", locked)
            logger.info("rename blocked")
            logger.info("still blocked, no retry storm")
        assert not list(tmp_path.glob("app.log.*"))
        clock.advance(lm.DailyRotatingFileHandler.ROTATION_RETRY_SECONDS + 1)
        logger.info("retried")
    finally:
        handler.close()
    rotated = (tmp_path / "app.log.2026-09-20").read_text(encoding="utf-8")
    assert "before midnight" in rotated and "rename blocked" in rotated
    assert "retried" in (tmp_path / "app.log").read_text(encoding="utf-8")


def test_idle_handler_is_rotated_by_maintenance_without_new_records(lm, tmp_path):
    clock = FakeClock(time.mktime((2026, 9, 20, 23, 59, 0, 0, 0, -1)))
    handler = lm.DailyRotatingFileHandler(tmp_path / "error.log", clock=clock)
    empty = lm.DailyRotatingFileHandler(tmp_path / "app.log", clock=clock)
    try:
        logger = _file_logger(lm, handler)
        logger.warning("late failure")
        assert handler.rotate_if_due() is False  # same day: nothing to do
        clock.advance(120)
        assert handler.rotate_if_due() is True   # no new record needed (error.log only sees WARNING+)
        assert handler.rotate_if_due() is False
        assert empty.rotate_if_due() is False    # nothing to archive
        logger.warning("next day")
    finally:
        handler.close()
        empty.close()
    assert "late failure" in (tmp_path / "error.log.2026-09-20").read_text(encoding="utf-8")
    active = (tmp_path / "error.log").read_text(encoding="utf-8")
    assert "next day" in active and "late failure" not in active
    assert not (tmp_path / "app.log.2026-09-20").exists()


def test_reopen_failure_after_rotation_never_raises_into_the_caller(lm, tmp_path, capsys):
    clock = FakeClock(time.mktime((2026, 9, 20, 23, 59, 0, 0, 0, -1)))
    handler = lm.DailyRotatingFileHandler(tmp_path / "app.log", clock=clock)
    try:
        logger = _file_logger(lm, handler)
        context = lm.LogContext(logger)
        context.info("before midnight")
        clock.advance(120)

        def denied():
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        handler._open = denied
        context.info("after midnight")  # must not raise into application code
        context.info("still locked")
        del handler._open
        context.info("recovered")
    finally:
        handler.close()
    assert "Logging error" in capsys.readouterr().err  # reported the logging way, not raised
    assert "before midnight" in (tmp_path / "app.log.2026-09-20").read_text(encoding="utf-8")
    assert "recovered" in (tmp_path / "app.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("age_days,archived_kept", [(30, False), (3, True)])
def test_stale_error_log_is_archived_and_aged_out_at_startup(tmp_path, age_days, archived_kept):
    """error.log gets no records at startup (only WARNING+): its old content must still be rotated and,
    past the retention window, deleted -- otherwise the viewer shows weeks-old errors as the newest."""
    logs = tmp_path / "runtime" / "logs"
    logs.mkdir(parents=True)
    now = time.time()
    stamp = now - age_days * 86400
    for name in ("error.log", "app.log", "launcher.log"):
        (logs / name).write_text(f"[2026-01-01 00:00:00] [ERROR] [jmconsole] old {name}\n", encoding="utf-8")
        os.utime(logs / name, (stamp, stamp))
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]);"
        "import core.path_utils as p; p.get_app_root = lambda: Path(sys.argv[2]);"
        "import core.logger as lm; assert lm.LOG_DIR == Path(sys.argv[2]) / 'runtime' / 'logs';"
        "lm.log.info('=== 启动 ==='); lm.start_log_maintenance(); lm.shutdown_logging()"
    )
    subprocess.run([sys.executable, "-c", code, str(REPO_ROOT), str(tmp_path)], check=True, timeout=60)
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    assert "old error.log" not in (logs / "error.log").read_text(encoding="utf-8")
    assert "old app.log" not in (logs / "app.log").read_text(encoding="utf-8")
    assert (logs / f"error.log.{day}").exists() is archived_kept
    assert (logs / f"app.log.{day}").exists() is archived_kept
    assert (logs / "launcher.log").exists() is archived_kept  # not written by this process: aged out


# ── retention ─────────────────────────────────────────────

def _make(path, age_days, now, size=10):
    path.write_bytes(b"x" * size)
    stamp = now - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_retention_deletes_only_old_non_active_log_files(lm, tmp_path):
    now = time.time()
    old_logs = ["app.log.2026-09-10", "error.log.2026-09-10", "launcher.log.2026-09-10",
                "app.log.2026-09-10.1", "app.legacy.20260910_120000.log",
                "launcher.log"]  # last written 30 days ago and not this process's output: all content expired
    kept = ["app.log", "error.log",                                # active, even when old
            "app.log.2026-09-23", "error.log.2026-09-23",          # recent
            "notes.txt", "app.log.bak", "readme.md"]              # not log files
    for name in old_logs:
        _make(tmp_path / name, 8, now)
    for name in kept:
        _make(tmp_path / name, 2 if "09-23" in name else 30, now)
    (tmp_path / "old.log").mkdir()
    result = lm.run_log_maintenance(tmp_path, now=now)
    assert sorted(result["deleted"]) == sorted(old_logs)
    assert result["failed"] == []
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(kept + ["old.log"])


def test_retention_keeps_launcher_log_this_server_writes_to(lm, tmp_path, monkeypatch):
    now = time.time()
    launcher_log = _make(tmp_path / "launcher.log", 30, now)  # the launcher redirected our stdout here
    real_fstat = os.fstat
    monkeypatch.setattr(os, "fstat", lambda fd: os.stat(launcher_log) if fd == 1 else real_fstat(fd))
    assert lm._is_own_output(launcher_log)
    result = lm.run_log_maintenance(tmp_path, now=now)
    assert result["deleted"] == [] and result["failed"] == [] and launcher_log.exists()


def test_retention_total_size_cap_deletes_oldest_first(lm, tmp_path):
    now = time.time()
    kb400 = 400 * 1024
    _make(tmp_path / "app.log.2026-09-21", 4, now, kb400)
    _make(tmp_path / "error.log.2026-09-22", 3, now, kb400)
    _make(tmp_path / "app.log.2026-09-23", 2, now, kb400)
    _make(tmp_path / "app.log", 5, now, kb400)  # active and oldest: never deleted
    result = lm.run_log_maintenance(tmp_path, now=now, max_total_mb=1)
    assert result["deleted"] == ["app.log.2026-09-21", "error.log.2026-09-22"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app.log", "app.log.2026-09-23"]
    assert result["total_bytes"] <= 1024 * 1024


def test_retention_skips_locked_files(lm, tmp_path, monkeypatch):
    now = time.time()
    locked = _make(tmp_path / "app.log.2026-09-01", 20, now)
    _make(tmp_path / "app.log.2026-09-02", 20, now)
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == locked.name:
            raise PermissionError("in use")
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    result = lm.run_log_maintenance(tmp_path, now=now)
    assert result["deleted"] == ["app.log.2026-09-02"]
    assert result["failed"] == [locked.name]
    assert locked.exists()


def test_start_log_maintenance_cleans_log_dir_but_keeps_active_files(lm):
    old = _make(lm.LOG_DIR / "app.log.2020-01-01", 30, time.time())
    lm.start_log_maintenance()
    assert not old.exists()
    assert lm.LOG_FILE.exists() and lm.ERROR_LOG_FILE.exists()


def test_readme_log_rotation_claims_match_the_code(lm):
    """launcher.log is archived by the launcher before a start (over LAUNCHER_LOG_MAX_BYTES or from an earlier
    day), not daily or at the app logs' 20 MB; the READMEs used to say the latter for all three files."""
    import launcher
    launcher_mb = launcher.LAUNCHER_LOG_MAX_BYTES // (1024 * 1024)
    en = " ".join((REPO_ROOT / "README.md").read_text(encoding="utf-8").split())
    zh = "".join((REPO_ROOT / "README.zh-CN.md").read_text(encoding="utf-8").split())
    assert f"`app.log` and `error.log` roll over daily or at {lm.MAX_ACTIVE_FILE_MB} MB" in en
    assert f"`launcher.log` is archived by the launcher before each start when it is over {launcher_mb} MB" in en
    assert "They roll over daily or at" not in en
    assert f"`app.log`和`error.log`每天或超过{lm.MAX_ACTIVE_FILE_MB}MB时归档" in zh
    assert f"`launcher.log`由启动器在每次启动前归档（超过{launcher_mb}MB" in zh
    # dedup: only ids written as *_id= are kept apart (the old wording promised all job/album/photo ids)
    assert "job/album/photo ids are kept apart" not in en and "任务/漫画/章节编号不会被合并" not in zh
    assert lm.message_template("Job job_3fa2b9c1d4e5 失败") == lm.message_template("Job job_00ff00ff00ff 失败")
    assert lm.message_template("失败 job_id=job_3fa2b9c1d4e5") != lm.message_template("失败 job_id=job_00ff00ff00ff")


def test_importing_logger_deletes_and_renames_nothing(tmp_path):
    logs = tmp_path / "runtime" / "logs"
    logs.mkdir(parents=True)
    now = time.time()
    names = ["app.log.2020-01-01", "app.legacy.20200101_000000.log", "error.log.2020-01-01"]
    for name in names:
        _make(logs / name, 400, now)
    (logs / "app.log").write_text("today\n", encoding="utf-8")
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]);"
        "import core.path_utils as p; p.get_app_root = lambda: Path(sys.argv[2]);"
        "import core.logger as lm; assert lm.LOG_DIR == Path(sys.argv[2]) / 'runtime' / 'logs'"
    )
    subprocess.run([sys.executable, "-c", code, str(REPO_ROOT), str(tmp_path)], check=True, timeout=60)
    assert sorted(p.name for p in logs.iterdir()) == sorted(names + ["app.log", "error.log"])
    assert (logs / "app.log").read_text(encoding="utf-8").startswith("today")


# ── global wiring ─────────────────────────────────────────

def test_app_logger_is_dedicated_and_does_not_propagate(lm):
    app_logger = logging.getLogger(lm.APP_LOGGER_NAME)
    assert lm.log._logger is app_logger
    assert app_logger.name != "jmcomic" and app_logger.propagate is False
    seen = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())
    capture = Capture()
    logging.getLogger().addHandler(capture)
    try:
        marker = _letters()
        lm.log.info(f"propagation check {marker}")
    finally:
        logging.getLogger().removeHandler(capture)
    assert not any(marker in message for message in seen)
    assert f"[INFO] [{lm.APP_LOGGER_NAME}] propagation check {marker}" in lm.LOG_FILE.read_text(encoding="utf-8")


def test_global_summary_written_on_shutdown_flush(lm):
    marker = _letters()
    for n in range(3):
        lm.log.warning(f"全局去重 {marker} n={n}")
    lm.flush_log_summaries(force=True)
    lines = [line for line in lm.ERROR_LOG_FILE.read_text(encoding="utf-8").splitlines() if marker in line]
    assert len(lines) == 2
    assert "↑ 同类消息 2 次已合并" in lines[1] and f"最后一条：全局去重 {marker} n=2" in lines[1]


def test_jmcomic_library_logger_is_claimed(lm, capsys):
    import jmcomic  # noqa: F401  (whatever the import order, the library logger ends up claimed)
    from jmcomic.jm_config import default_jm_logging
    jm_logger = logging.getLogger("jmcomic")
    jm_logger.addHandler(logging.StreamHandler(sys.stdout))  # what setup_default_jm_logger()/enable_pretty_log() add
    lm.claim_library_loggers()
    foreign = [h for h in jm_logger.handlers
               if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               and not getattr(h, "_jm_owned", False)]
    assert foreign == []
    assert not any(getattr(h, "stream", None) is sys.stdout for h in jm_logger.handlers)
    assert jm_logger.propagate is False

    kept, dropped = _letters(), _letters()
    default_jm_logging("req.retry", f"次数: [1/5], 域名: example {kept}")
    default_jm_logging("image.before", f"图片准备下载: 00001.jpg {dropped}")
    app_text = lm.LOG_FILE.read_text(encoding="utf-8")
    assert f"[WARNING] [jmcomic] 【req.retry】次数: [1/5], 域名: example {kept}" in app_text
    assert kept in lm.ERROR_LOG_FILE.read_text(encoding="utf-8")
    assert dropped not in app_text
    output = capsys.readouterr()
    assert kept not in output.out and kept not in output.err


def test_flush_endpoint_writes_pending_summaries(client):
    """launcher.py calls this before taskkill /F, which skips atexit and would lose the counts."""
    from core import logger as lm
    marker = _letters()
    for n in range(3):
        lm.log.warning(f"停止前刷新 {marker} n={n}")
    response = client.post("/api/system/logs/flush")
    assert response.status_code == 200 and response.get_json()["status"] == "ok"
    lines = [line for line in lm.ERROR_LOG_FILE.read_text(encoding="utf-8").splitlines() if marker in line]
    assert len(lines) == 2 and "↑ 同类消息 2 次已合并" in lines[1]
    assert client.post("/api/system/logs/flush", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_flush_endpoint_leaves_windows_without_repeats_open(client):
    """If the server keeps running after the call (taskkill failed, another caller), a message logged once
    before it must still be merged afterwards instead of being written in full again."""
    from core import logger as lm
    marker = _letters()
    lm.log.warning(f"刷新后窗口 {marker}")
    assert client.post("/api/system/logs/flush").get_json()["flushed"] == 0
    lm.log.warning(f"刷新后窗口 {marker}")
    lm.log.warning(f"刷新后窗口 {marker}")
    lm.flush_log_summaries(force=True)
    lines = [line for line in lm.ERROR_LOG_FILE.read_text(encoding="utf-8").splitlines() if marker in line]
    assert len(lines) == 2 and "↑ 同类消息 2 次已合并" in lines[1]


def test_forged_line_break_in_request_path_stays_one_viewer_entry(client):
    marker = _letters()
    forged = f"/x%0A[2026-09-25%2003:00:00]%20[ERROR]%20[jmconsole]%20forged-{marker}"
    assert client.get(forged, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    data = client.get("/api/system/logs", query_string={"source": "app", "q": f"forged-{marker}"}).get_json()
    assert len(data["entries"]) == 1
    entry = data["entries"][0]
    assert entry["level"] == "INFO" and entry["time"] != "2026-09-25 03:00:00"
    assert f"HTTP GET /x\\n[2026-09-25 03:00:00] [ERROR] [jmconsole] forged-{marker} → 403 FORBIDDEN" in entry["message"]


def test_request_log_skips_static_and_health(client):
    from core import logger as lm
    assert client.application.logger.propagate is False
    assert client.get("/static/css/style.css").status_code == 200
    assert client.get("/api/system/health").status_code == 200
    missing = f"/api/{_letters()}"
    assert client.get(missing).status_code == 404
    text = lm.LOG_FILE.read_text(encoding="utf-8")
    assert "HTTP GET /static/" not in text
    assert "HTTP GET /api/system/health" not in text
    assert f"[INFO] [{lm.APP_LOGGER_NAME}] HTTP GET {missing} → 404 NOT FOUND (" in text
