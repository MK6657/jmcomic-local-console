"""Settings-page log viewer: GET /api/system/logs and POST /api/system/logs/open-folder.

Every test writes its own sample logs into tmp_path and points core.logger.LOG_DIR there;
the real runtime/logs is never touched. App modules are imported inside fixtures only.
"""
import builtins
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RID_A = "req_ab12cd34"
RID_B = "req_zz99yy88"


def line(ts, level, message, extra=None, name="jmcomic"):
    text = f"[{ts}] [{level}] [{name}] {message}"
    if extra is not None:
        text += " " + json.dumps(extra, ensure_ascii=False)
    return text


def write_log(path, lines, newline="\n"):
    path.write_bytes((newline.join(lines) + newline).encode("utf-8"))


@pytest.fixture
def logdir(client, tmp_path, monkeypatch):
    """Isolated log directory; also patches the module-level copy used by /api/system/diagnose."""
    import core.logger as core_logger
    import routes.api_system as api_system
    folder = tmp_path / "logs"
    folder.mkdir()
    monkeypatch.setattr(core_logger, "LOG_DIR", folder)
    monkeypatch.setattr(api_system, "LOG_DIR", folder)
    return folder


@pytest.fixture
def reads(monkeypatch):
    """Records which files the log API opens and how many bytes it reads from them."""
    import routes.api_system as api_system
    record = {"names": [], "bytes": 0}
    real_open = builtins.open

    class Counting:
        def __init__(self, fh):
            self._fh = fh

        def read(self, size=-1):
            data = self._fh.read(size)
            record["bytes"] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

    def counting_open(path, mode="r", *args, **kwargs):
        record["names"].append(Path(path).name)
        return Counting(real_open(path, mode, *args, **kwargs))

    monkeypatch.setattr(api_system, "open", counting_open, raising=False)
    return record


def get_logs(client, **params):
    response = client.get("/api/system/logs", query_string=params)
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()
    assert data["status"] == "ok"
    return data


def test_newest_first_with_request_id_and_extra(client, logdir):
    write_log(logdir / "error.log", [
        line("2026-09-25 10:00:01", "WARNING", "封面加载慢 album_id=1", {"request_id": RID_A, "album_id": "1"}),
        line("2026-09-25 10:00:02", "ERROR", f"下载失败 request_id={RID_B}"),
        line("2026-09-25 10:00:03", "WARNING", "搜索超时", {"request_id": RID_A}),
    ])
    data = get_logs(client)
    assert data["source"] == "error"
    assert [e["time"] for e in data["entries"]] == [
        "2026-09-25 10:00:03", "2026-09-25 10:00:02", "2026-09-25 10:00:01"]
    newest, middle, oldest = data["entries"]
    assert newest == {"time": "2026-09-25 10:00:03", "level": "WARNING", "logger": "jmcomic",
                      "message": "搜索超时", "request_id": RID_A}  # no leftover extra -> no "extra" key
    assert middle["level"] == "ERROR" and middle["request_id"] == RID_B  # found in the message text
    assert oldest["message"] == "封面加载慢 album_id=1"
    assert oldest["extra"] == {"album_id": "1"} and oldest["request_id"] == RID_A


def test_response_metadata(client, logdir):
    import core.logger as core_logger
    write_log(logdir / "error.log", [line("2026-09-25 10:00:00", "ERROR", "x")])
    write_log(logdir / "app.log", [line("2026-09-25 10:00:00", "INFO", "y" * 2048)])
    data = get_logs(client)
    assert data["retention_days"] == core_logger.MAX_LOG_AGE_DAYS
    assert data["log_dir"] == str(logdir)
    names = {f["name"] for f in data["files"]}
    assert names == {"error.log", "app.log"}
    assert all(set(f) == {"name", "size_kb", "modified"} for f in data["files"])
    assert data["total_size_mb"] >= 0 and data["truncated"] is False


def test_source_selects_file_and_rejects_unknown(client, logdir):
    write_log(logdir / "error.log", [line("2026-09-25 10:00:00", "ERROR", "only problems")])
    write_log(logdir / "app.log", [
        line("2026-09-25 09:59:59", "INFO", "started"),
        line("2026-09-25 10:00:00", "ERROR", "only problems"),
    ])
    assert [e["message"] for e in get_logs(client)["entries"]] == ["only problems"]
    assert [e["message"] for e in get_logs(client, source="app")["entries"]] == ["only problems", "started"]
    assert client.get("/api/system/logs?source=launcher").status_code == 400
    assert client.get("/api/system/logs?source=../error").status_code == 400


def test_query_is_case_insensitive_substring(client, logdir):
    write_log(logdir / "app.log", [
        line("2026-09-25 10:00:00", "INFO", "搜索 keyword=Naruto", {"request_id": RID_A}),
        line("2026-09-25 10:00:01", "WARNING", "Connection TIMEOUT for album 42", {"request_id": RID_B}),
        line("2026-09-25 10:00:02", "ERROR", "解析失败"),
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ValueError: Deep Inside Traceback",
    ])
    assert [e["message"] for e in get_logs(client, source="app", q="timeout")["entries"]] == [
        "Connection TIMEOUT for album 42"]
    assert [e["time"] for e in get_logs(client, source="app", q="NARUTO")["entries"]] == ["2026-09-25 10:00:00"]
    assert [e["request_id"] for e in get_logs(client, source="app", q=RID_B.upper())["entries"]] == [RID_B]
    assert [e["time"] for e in get_logs(client, source="app", q="deep inside")["entries"]] == ["2026-09-25 10:00:02"]
    assert get_logs(client, source="app", q="no such text")["entries"] == []


def test_limit_default_clamp_and_invalid(client, logdir):
    write_log(logdir / "app.log", [line(f"2026-09-25 10:{i // 60:02d}:{i % 60:02d}", "INFO", f"n{i}") for i in range(600)])
    assert [e["message"] for e in get_logs(client, source="app", limit=3)["entries"]] == ["n599", "n598", "n597"]
    assert len(get_logs(client, source="app")["entries"]) == 200
    assert len(get_logs(client, source="app", limit="abc")["entries"]) == 200
    assert len(get_logs(client, source="app", limit=0)["entries"]) == 1
    assert len(get_logs(client, source="app", limit=100000)["entries"]) == 500


def test_continuation_lines_attach_to_previous_entry(client, logdir):
    write_log(logdir / "error.log", [
        line("2026-09-25 10:00:00", "WARNING", "before"),
        line("2026-09-25 10:00:01", "ERROR", "任务异常 job_id=job_1"),
        "Traceback (most recent call last):",
        '  File "core/job_manager.py", line 10, in run',
        "    raise ValueError('boom')",
        "ValueError: boom " + json.dumps({"request_id": RID_A, "job_id": "job_1"}),
        line("2026-09-25 10:00:02", "WARNING", "after"),
    ])
    entries = get_logs(client)["entries"]
    assert [e["message"].split("\n")[0] for e in entries] == ["after", "任务异常 job_id=job_1", "before"]
    failure = entries[1]
    assert failure["message"].splitlines() == [
        "任务异常 job_id=job_1",
        "Traceback (most recent call last):",
        '  File "core/job_manager.py", line 10, in run',
        "    raise ValueError('boom')",
        "ValueError: boom",
    ]
    assert failure["request_id"] == RID_A and failure["extra"] == {"job_id": "job_1"}


def test_malformed_lines_crlf_and_bad_bytes_are_tolerated(client, logdir):
    raw = "\r\n".join([
        "garbage before any header",
        line("2026-09-25 10:00:00", "WARNING", "first"),
        "[not a real header line",
        "",
        line("2026-09-25 10:00:01", "ERROR", "second {not json}"),
        line("2026-09-25 10:00:02", "WARNING", "↑ 同类消息 12 次已合并（10:01:02–10:01:40），最后一条：请求失败",
             {"request_id": RID_A}),
    ]).encode("utf-8") + b"\r\n" + b"[2026-09-25 10:00:03] [ERROR] [jmcomic] bad bytes \xff\xfe end\r\n"
    (logdir / "error.log").write_bytes(raw)
    entries = get_logs(client)["entries"]
    assert [e["time"] for e in entries] == [
        "2026-09-25 10:00:03", "2026-09-25 10:00:02", "2026-09-25 10:00:01", "2026-09-25 10:00:00", ""]
    assert entries[0]["message"].startswith("bad bytes ") and entries[0]["message"].endswith(" end")
    assert entries[1]["message"].startswith("↑ 同类消息 12 次已合并") and entries[1]["level"] == "WARNING"
    assert entries[2]["message"] == "second {not json}" and "extra" not in entries[2]
    assert entries[3]["message"] == "first\n[not a real header line"  # malformed line kept as continuation
    assert entries[4]["message"] == "garbage before any header" and entries[4]["level"] == ""
    assert all("\r" not in e["message"] for e in entries)


def test_block_boundaries_do_not_change_parsing(client, logdir, monkeypatch):
    import routes.api_system as api_system
    lines = []
    for i in range(40):
        lines.append(line(f"2026-09-25 10:00:{i:02d}", "ERROR", f"失败 {i} 漫画《标题{i}》", {"request_id": RID_A, "i": i}))
        if i % 3 == 0:
            lines += ["Traceback (most recent call last):", f"  frame {i}", f"OSError: {i}"]
    write_log(logdir / "error.log", lines)
    expected = get_logs(client, limit=500)["entries"]
    assert len(expected) == 40
    monkeypatch.setattr(api_system, "_LOG_BLOCK_BYTES", 37)  # splits lines and UTF-8 characters
    assert get_logs(client, limit=500)["entries"] == expected


class _CountingHeader:
    """Wraps the log header regex and counts how many lines the reader tests against it."""

    def __init__(self, pattern):
        self.pattern = pattern
        self.calls = 0

    def match(self, text):
        self.calls += 1
        return self.pattern.match(text)


def test_long_record_without_headers_is_scanned_once(client, logdir, monkeypatch):
    """A multi-block run of continuation lines (an HTML response body, a huge traceback) used to be decoded and
    matched again for every further block: budget²/block work, 3 s and ~140 MB for 16 MB. Each line is now
    tested once, and the output does not change."""
    import routes.api_system as api_system
    body = [f"    <div class=\"thumb\"><img src=\"/media/albums/{n}_3x4.jpg\" alt=\"测试\"></div>" for n in range(3000)]
    write_log(logdir / "error.log", [
        line("2026-09-20 10:00:00", "INFO", "older record"),
        line("2026-09-20 10:00:01", "ERROR", "请求失败 resp=<!DOCTYPE html>"),
        *body,
        line("2026-09-25 10:00:00", "ERROR", "newest record", {"request_id": RID_A}),
    ], newline="\r\n")
    expected = get_logs(client, limit=500)["entries"]
    assert [e["message"].split("\n")[0] for e in expected] == [
        "newest record", "请求失败 resp=<!DOCTYPE html>", "older record"]
    assert expected[1]["message"].split("\n")[1:3] == body[:2] and expected[1]["message"].endswith("…（已截断）")
    counter = _CountingHeader(api_system._LOG_HEADER_RE)
    monkeypatch.setattr(api_system, "_LOG_HEADER_RE", counter)
    monkeypatch.setattr(api_system, "_LOG_BLOCK_BYTES", 1024)  # ~250 blocks without a header
    assert get_logs(client, limit=500)["entries"] == expected
    total_lines = len(body) + 3
    assert counter.calls < 2 * total_lines, counter.calls  # was ~250 times the region's line count


def test_headerless_lines_at_file_start_still_become_entries(client, logdir, monkeypatch):
    """Lines before the first header of a file are shown one entry each, however many blocks they span."""
    import routes.api_system as api_system
    stray = [f"stray line {n:04d} " + "y" * 40 for n in range(300)]
    write_log(logdir / "app.log", stray + [line("2026-09-25 10:00:00", "INFO", "first real record")])
    expected = get_logs(client, source="app", limit=500)["entries"]
    assert [e["message"] for e in expected] == ["first real record"] + stray[::-1]
    monkeypatch.setattr(api_system, "_LOG_BLOCK_BYTES", 97)
    assert get_logs(client, source="app", limit=500)["entries"] == expected


def test_newer_search_from_the_same_viewer_stops_the_older_scan(client, logdir, monkeypatch):
    """The browser aborts the previous fetch on every debounced keystroke, but the server kept scanning: slow
    typing stacked several full 16 MB scans that slowed each other (and every other request) down."""
    import threading
    import routes.api_system as api_system
    monkeypatch.setattr(api_system, "_LOG_BLOCK_BYTES", 4096)
    write_log(logdir / "app.log", [line("2026-09-25 10:00:00", "INFO", f"entry {n:05d} " + "x" * 80)
                                   for n in range(3000)])
    size = (logdir / "app.log").stat().st_size
    worker_reads = {"bytes": 0}
    first_block, newer_done = threading.Event(), threading.Event()
    real_open = builtins.open

    class PausingFile:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            data = self._handle.read(size)
            worker_reads["bytes"] += len(data)
            if not first_block.is_set():
                first_block.set()
                assert newer_done.wait(10)  # the user types the next character meanwhile
            return data

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()

    def open_for_test(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return PausingFile(handle) if threading.current_thread().name == "older-search" else handle
    monkeypatch.setattr(api_system, "open", open_for_test, raising=False)
    viewer = "viewer-" + os.urandom(4).hex()
    older = {}

    def search_in_background():
        older["response"] = client.application.test_client().get("/api/system/logs", query_string={
            "source": "app", "q": "no-such-text", "limit": 500, "viewer": viewer, "seq": 1})  # a full scan
    thread = threading.Thread(target=search_in_background, name="older-search")
    thread.start()
    assert first_block.wait(10)
    newer = get_logs(client, source="app", q="entry 02999", viewer=viewer, seq=2)
    newer_done.set()
    thread.join(10)
    assert [e["message"] for e in newer["entries"]] == ["entry 02999 " + "x" * 80]
    response = older["response"]
    assert response.status_code == 409 and response.get_json()["superseded"] is True
    assert worker_reads["bytes"] <= 2 * api_system._LOG_BLOCK_BYTES < size  # stopped at the next block


def test_only_a_newer_read_of_the_same_viewer_cancels(client):
    import routes.api_system as api_system
    viewer, other = "v-" + os.urandom(4).hex(), "v-" + os.urandom(4).hex()
    first = api_system._claim_log_read(viewer, 5)
    elsewhere = api_system._claim_log_read(other, 1)
    anonymous = api_system._claim_log_read("", 0)
    assert first() and elsewhere() and anonymous()
    second = api_system._claim_log_read(viewer, 6)
    assert not first() and second() and elsewhere() and anonymous()  # other tabs and scripts are unaffected
    late = api_system._claim_log_read(viewer, 5)  # an older request that reached the server late
    assert not late() and second()
    data = get_logs(client, source="app", viewer=viewer, seq=7)  # the viewer's own newest read is served
    assert data["status"] == "ok"
    assert client.get("/api/system/logs", query_string={"viewer": viewer, "seq": 3}).status_code == 409
    assert client.get("/api/system/logs", query_string={"viewer": "../x", "seq": 3}).status_code == 200


def test_partial_first_line_is_dropped_when_cap_is_hit(client, logdir, monkeypatch):
    import routes.api_system as api_system
    write_log(logdir / "error.log", [line(f"2026-09-25 10:00:{i:02d}", "ERROR", f"entry-{i:02d}") for i in range(50)])
    write_log(logdir / "error.log.2026-09-01", [line("2026-09-01 10:00:00", "ERROR", "rotated")])
    monkeypatch.setattr(api_system, "_LOG_READ_CAP_BYTES", 200)
    data = get_logs(client, limit=500)
    assert data["truncated"] is True
    assert 1 <= len(data["entries"]) < 50
    assert data["entries"][0]["message"] == "entry-49"
    assert all(e["time"].startswith("2026-09-25 10:00:") and e["message"].startswith("entry-") for e in data["entries"])


def test_big_file_reads_only_the_tail_and_respects_byte_cap(client, logdir, reads, monkeypatch):
    import routes.api_system as api_system
    monkeypatch.setattr(api_system, "_LOG_QUERY_READ_CAP_BYTES", api_system._LOG_READ_CAP_BYTES)
    filler = "x" * 100
    lines = [line("2026-09-20 00:00:00", "WARNING", "OLDEST-MARKER")]
    lines += [line("2026-09-25 10:00:00", "WARNING", f"entry {i:06d} {filler}") for i in range(45000)]
    write_log(logdir / "error.log", lines)
    assert (logdir / "error.log").stat().st_size > api_system._LOG_READ_CAP_BYTES + 512 * 1024

    data = get_logs(client)
    assert len(data["entries"]) == 200 and data["entries"][0]["message"].startswith("entry 044999 ")
    assert data["truncated"] is False
    assert reads["bytes"] <= api_system._LOG_BLOCK_BYTES  # the newest 200 entries fit in one tail block

    reads["bytes"] = 0
    data = get_logs(client, q="OLDEST-MARKER")
    assert data["entries"] == [] and data["truncated"] is True
    assert data["read_limit_mb"] == 4  # the UI must say "not found in the last 4 MB", not "not found"
    assert reads["bytes"] <= api_system._LOG_READ_CAP_BYTES


def test_search_reads_further_back_than_the_plain_view(client, logdir, monkeypatch):
    import routes.api_system as api_system
    monkeypatch.setattr(api_system, "_LOG_READ_CAP_BYTES", 4096)
    monkeypatch.setattr(api_system, "_LOG_QUERY_READ_CAP_BYTES", 256 * 1024)
    write_log(logdir / "app.log.2026-09-23", [
        line("2026-09-23 10:00:00", "ERROR", "取图失败", {"request_id": RID_A}, name="jmconsole")])
    write_log(logdir / "app.log", [line("2026-09-25 10:00:00", "INFO", "x" * 200) for _ in range(100)])
    plain = get_logs(client, source="app", limit=500)
    assert plain["truncated"] is True and plain["read_limit_mb"] == round(4096 / 1024 / 1024, 2)
    traced = get_logs(client, source="app", q=RID_A)  # "只看此请求" on a record from two days ago
    assert [e["message"] for e in traced["entries"]] == ["取图失败"] and traced["truncated"] is False
    assert traced["read_limit_mb"] == 0.25


def test_size_rotated_files_are_read_in_order(client, logdir):
    """The real handler names a second rotation on the same day <base>.<day>.1, .2 ... (higher is newer)."""
    import logging
    import core.logger as core_logger
    handler = core_logger.DailyRotatingFileHandler(logdir / "app.log", max_bytes=600)
    handler.setFormatter(core_logger.StructuredFormatter(core_logger.LOG_FORMAT, datefmt=core_logger.DATE_FORMAT))
    logger = logging.getLogger("viewer-rotation-test")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        for n in range(200):
            logger.info(f"event seq={n:04d}")
    finally:
        handler.close()
        logger.handlers[:] = []
    names = [p.name for p in logdir.iterdir()]
    assert any(name.endswith(".10") for name in names), names  # .10 must sort after .9
    data = get_logs(client, source="app", limit=500)
    assert [e["message"] for e in data["entries"]] == [f"event seq={n:04d}" for n in range(199, -1, -1)]
    assert data["truncated"] is False
    assert [e["message"] for e in get_logs(client, source="app", q="seq=0100")["entries"]] == ["event seq=0100"]


def test_rotation_after_the_size_cap_deleted_older_archives_stays_newest_first(client, logdir):
    """After the total-size cap deleted a day's oldest archives (<base>.<day>, .1 ...), the next rotation used to
    take the first free name: the newest content landed in <base>.<day> and was read after the older .2/.3, so
    the viewer skipped the newest rotated records and searches spent their budget on older files."""
    import logging
    import re
    import time
    import core.logger as core_logger
    removed = []

    def first_seq(path):
        return int(re.search(r"seq=(\d{4})", path.read_text(encoding="utf-8")).group(1))

    def size_cap():  # what run_log_maintenance's total cap does: delete the oldest archives first
        archives = sorted(logdir.glob("app.log.*"), key=first_seq)
        while len(archives) > 3:
            oldest = archives.pop(0)
            removed.append(oldest.name)
            oldest.unlink()
    noon = time.mktime((2026, 9, 24, 12, 0, 0, 0, 0, -1))
    handler = core_logger.DailyRotatingFileHandler(logdir / "app.log", max_bytes=600, on_rotate=size_cap,
                                                   clock=lambda: noon)
    handler.setFormatter(core_logger.StructuredFormatter(core_logger.LOG_FORMAT, datefmt=core_logger.DATE_FORMAT))
    logger = logging.getLogger("viewer-rotation-cap-test")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        for n in range(120):
            logger.info(f"event seq={n:04d}")
    finally:
        handler.close()
        logger.handlers[:] = []
    assert "app.log.2026-09-24" in removed and "app.log.2026-09-24.1" in removed
    archives = sorted(logdir.glob("app.log.*"), key=first_seq)  # oldest content first
    numbers = [int(re.fullmatch(r"app\.log\.2026-09-24(?:\.(\d+))?", p.name).group(1) or 0) for p in archives]
    assert numbers == sorted(numbers)  # a higher suffix always means newer content
    data = get_logs(client, source="app", limit=500)
    seqs = [int(e["message"].split("=")[1]) for e in data["entries"]]
    assert seqs == list(range(119, 119 - len(seqs), -1))  # newest first, no jump back to older files
    assert seqs[-1] == first_seq(archives[0])


def test_rotated_file_order_is_date_then_number(logdir):
    import routes.api_system as api_system
    for name in ("app.log", "app.log.2026-09-24", "app.log.2026-09-24.2", "app.log.2026-09-24.10",
                 "app.log.2026-09-25", "app.log.2026-09-25.1", "app.log.2026-09-23.1", "app.log.2026-09-24.x"):
        write_log(logdir / name, [line("2026-09-25 10:00:00", "INFO", name)])
    assert [p.name for p in api_system._log_files_for("app", logdir)] == [
        "app.log", "app.log.2026-09-25.1", "app.log.2026-09-25", "app.log.2026-09-24.10",
        "app.log.2026-09-24.2", "app.log.2026-09-24", "app.log.2026-09-23.1"]


def test_only_app_and_error_logs_are_read(client, logdir, reads, tmp_path):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    older = (date.today() - timedelta(days=2)).isoformat()
    marker = "SECRET-MARKER"
    write_log(logdir / "error.log", [line(f"{date.today()} 10:00:00", "ERROR", f"{marker} current")])
    write_log(logdir / f"error.log.{yesterday}", [line(f"{yesterday} 10:00:00", "ERROR", f"{marker} yesterday")])
    write_log(logdir / f"error.log.{older}", [line(f"{older} 10:00:00", "ERROR", f"{marker} older")])
    for name in ("launcher.log", f"launcher.log.{yesterday}", "error.log.bak", "app.legacy.20260701.log",
                 "error.log.2026-09-24.txt", "notes.txt"):
        write_log(logdir / name, [line(f"{date.today()} 11:00:00", "ERROR", f"{marker} from {name}")])
    (logdir / "sub").mkdir()
    write_log(logdir / "sub" / "error.log", [line(f"{date.today()} 12:00:00", "ERROR", f"{marker} nested")])
    write_log(tmp_path / "error.log", [line(f"{date.today()} 12:00:00", "ERROR", f"{marker} outside")])

    data = get_logs(client, q=marker, path=str(tmp_path / "error.log"), file="../error.log")
    assert [e["message"] for e in data["entries"]] == [
        f"{marker} current", f"{marker} yesterday", f"{marker} older"]
    assert set(reads["names"]) == {"error.log", f"error.log.{yesterday}", f"error.log.{older}"}
    assert "launcher.log" in {f["name"] for f in data["files"]}  # listed (size only), never read


def test_missing_log_directory_is_empty_not_an_error(client, logdir, monkeypatch):
    import core.logger as core_logger
    monkeypatch.setattr(core_logger, "LOG_DIR", logdir / "does-not-exist")
    data = get_logs(client, source="app")
    assert data["entries"] == [] and data["files"] == [] and data["total_size_mb"] == 0


def test_open_folder_uses_startfile(client, logdir, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "startfile", lambda target: calls.append(target), raising=False)
    response = client.post("/api/system/logs/open-folder")
    assert response.status_code == 200 and response.get_json()["status"] == "ok"
    assert calls == [str(logdir)]


def test_open_folder_failure_and_unsupported_platform(client, logdir, monkeypatch):
    def broken(_target):
        raise OSError("no shell")
    monkeypatch.setattr(os, "startfile", broken, raising=False)
    assert client.post("/api/system/logs/open-folder").status_code == 500
    monkeypatch.delattr(os, "startfile", raising=False)
    assert client.post("/api/system/logs/open-folder").status_code == 501


def test_open_folder_rejects_cross_site_requests(client, logdir, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "startfile", lambda target: calls.append(target), raising=False)
    response = client.post("/api/system/logs/open-folder", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403 and calls == []


def test_logs_js_renders_log_text_without_html_injection():
    import re
    source = (ROOT / "static" / "js" / "logs.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment",
                 "DOMParser", "jQuery", "$("):
        assert sink not in source, f"logs.js must not use {sink}"
    assert "textContent" in code
    assert "AbortError" in code and "window.apiFetch" in code


def test_logs_js_follows_the_truncation_and_bfcache_contract():
    import re
    source = (ROOT / "static" / "js" / "logs.js").read_text(encoding="utf-8")
    code = re.sub(r"(?m)^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
    # an empty result from a truncated read is not "nothing found": the empty state checks truncated
    # and states the server's actual read limit instead of a hard-coded size
    empty = code[code.index("function emptyText"):code.index("function renderEntries")]
    assert "data.truncated" in empty and "readLimitText(data)" in empty
    assert "read_limit_mb" in code and "4 MB" not in code
    assert "setStatus('empty', emptyText(data), false, !!data.truncated)" in code
    # a read aborted by pagehide is retried when the page comes back from the back/forward cache
    assert "addEventListener('pageshow'" in code and "event.persisted && state.interrupted" in code


def test_logs_js_lets_the_server_cancel_superseded_searches_and_waits_for_ime():
    import re
    source = (ROOT / "static" / "js" / "logs.js").read_text(encoding="utf-8")
    code = re.sub(r"(?m)^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
    # every read names its page instance and sequence number, so the server can stop the older scans
    assert "params.set('viewer', VIEWER_ID)" in code and "params.set('seq', String(seq))" in code
    # pinyin typed through an IME must not start a full log scan per letter
    handler = code[code.index("addEventListener('input'"):code.index("addEventListener('keydown'")]
    assert "composing || e.isComposing" in handler
    assert "addEventListener('compositionstart'" in code and "addEventListener('compositionend'" in code
    keydown = code[code.index("addEventListener('keydown'"):]
    assert "!e.isComposing" in keydown[:400] and "229" in keydown[:400]


def test_settings_page_contains_log_section_outside_form(client):
    html = client.get("/settings").get_data(as_text=True)
    assert 'id="logs"' in html
    section = html.index('id="logs"')
    utils_script = html.index('src="/static/js/utils.js')
    assert html.index("</form>") < section < utils_script
    assert utils_script < html.index('src="/static/js/logs.js')
    for element_id in ("log-search", "log-refresh-btn", "log-open-folder-btn", "log-list", "log-status"):
        assert html.index(f'id="{element_id}"') > section
    assert 'data-log-source="error"' in html and 'data-log-source="app"' in html
