"""F13: real temporary SQLite, real transfer gates, controlled synchronous IO only.

The suite-wide socket/libcurl guards remain enabled. No Future or detached HTTP
worker stands in for transport completion, and no fake-client budget exemption is
used. Clients below expose the same postman/session boundary as production.
"""
import json
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


class Task:
    def __init__(self, fn):
        self.result = self.error = None
        self.done = threading.Event()

        def run():
            try:
                self.result = fn()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self):
        self.thread.join(2)
        assert not self.thread.is_alive(), "controlled work did not exit"
        return self


class Transport:
    def __init__(self, harness):
        from curl_cffi.requests import RetryStrategy
        self.harness = harness
        self.original_options = {12345: "preserve"}
        self.session = SimpleNamespace(curl_options=self.original_options,
                                       retry=RetryStrategy(count=2), close=self.close)
        self.original_retry = self.session.retry
        self.calls = []
        self.in_request = False
        self.closed = False
        self.close_thread = None

    def request(self, method, url, **kwargs):
        assert not self.closed
        self.calls.append((method, url, kwargs, dict(self.session.curl_options)))
        self.in_request = True
        try:
            return self.harness.answer(method, url, **kwargs)
        finally:
            self.in_request = False

    def get(self, url, **kwargs):
        return self.request("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("post", url, **kwargs)

    def close(self):
        assert not self.in_request, "curl closed while original request was alive"
        assert self.session.curl_options is self.original_options
        assert self.session.retry is self.original_retry
        assert "get" not in self.__dict__ and "post" not in self.__dict__
        if self.harness.on_close:
            self.harness.on_close()
        self.closed = True
        self.close_thread = threading.get_ident()


class Client:
    def __init__(self, harness, timeout):
        self.root = Transport(harness)
        self.timeout = timeout
        self.domains = list(harness.domains)
        self.selected = []

    def get_root_postman(self):
        return self.root

    def get_domain_list(self):
        return list(self.domains)

    def set_domain_list(self, domains):
        self.selected.append(list(domains))

    def get_album_detail(self, album_id):
        return self.root.get(f"https://{self.selected[-1][0]}/album/{album_id}", timeout=600)


class Harness:
    def __init__(self, checker, db, root):
        self.checker, self.db, self.root = checker, db, root
        self.clients, self.tasks, self.gates = [], [], []
        self.domains = ["one.invalid", "two.invalid", "three.invalid", "four.invalid"]
        self.answer = lambda *args, **kwargs: self.album()
        self.on_close = None

    @staticmethod
    def album(album_id="3001"):
        return SimpleNamespace(album_id=album_id, episode_list=[("71", 1, "one"), ("72", 2, "two")])

    def make_album(self, album_id="3001"):
        from core import local_availability, update_store
        folder = self.root / album_id
        chapter = folder / "one__71"
        chapter.mkdir(parents=True)
        (chapter / "001.webp").write_bytes(b"local page")
        (chapter / ".jm-chapter.json").write_text(json.dumps({"photo_id": "71", "format": 2}), encoding="utf-8")
        self.db.insert_job("local_" + album_id, album_id, "local", ["71"])
        assert self.db.transition_job_status("local_" + album_id, ["queued"], "completed",
                                             output_path=str(folder), completed_at=datetime.now().isoformat())
        update_store.note_download_started(album_id, [("71", 1, "one")], had_local_content=False)
        self.db.set_cached_album_detail(album_id, '{"cached": true}')
        local_availability.forget(folder)
        assert local_availability.is_readable(album_id)

    def new_client(self, timeout):
        client = Client(self, timeout)
        self.clients.append(client)
        return client

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def launch(self, fn):
        task = Task(fn)
        self.tasks.append(task)
        return task

    def block_fetch(self, *, fail=False):
        entered, release = self.gate(), self.gate()

        def answer(*args, **kwargs):
            entered.set()
            assert release.wait(3), "test did not release transport"
            if fail:
                raise ConnectionError("controlled offline failure")
            return self.album()

        self.answer = answer
        return entered, release

    def snapshot(self):
        conn = self.db.get_db()
        try:
            return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                    for table in ("album_update_checks", "update_check_log", "album_detail_cache", "jobs")}
        finally:
            conn.close()

    def active(self, count):
        deadline = time.monotonic() + 2
        while self.checker.runtime_status()["active_checks"] != count:
            assert time.monotonic() < deadline
            threading.Event().wait(0.005)


@pytest.fixture
def checks(client, tmp_path, monkeypatch):
    from core import database as db, jm_service, path_guard, update_checker
    assert db.DB_PATH.is_relative_to(tmp_path)
    update_checker._reset_for_tests()
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    harness = Harness(update_checker, db, root)
    harness.real_new_client = jm_service.new_check_client
    monkeypatch.setattr(jm_service, "new_check_client", harness.new_client)
    monkeypatch.setattr(update_checker, "STARTUP_DELAY", 3600)
    monkeypatch.setattr(update_checker, "STARTUP_JITTER", 0)
    yield harness
    for gate in harness.gates:
        gate.set()
    for task in harness.tasks:
        task.join()
    assert update_checker.stop(timeout=2)
    update_checker._reset_for_tests()


@pytest.mark.parametrize("trigger", ["manual", "auto"])
def test_request_stop_closes_admission_before_any_local_or_upstream_work(checks, client, monkeypatch, trigger):
    from core import local_availability
    checker = checks.checker
    checker.request_stop()
    monkeypatch.setattr(local_availability, "is_readable", lambda *a: pytest.fail("admitted after stop"))
    with pytest.raises(checker.CheckStopped):
        checker.check_album("3001", trigger)
    with pytest.raises(checker.CheckStopped):
        checker.tick()
    response = client.post("/api/updates/3001/check")
    assert response.status_code == 503 and response.json["reason"] == "stopped"
    assert checks.clients == [] and checker.stop(timeout=0)


def test_factory_default_permits_manual_check_without_start(checks, client):
    from core import update_store
    checks.make_album()
    assert not checks.checker.runtime_status()["armed"]
    response = client.post("/api/updates/3001/check")
    assert response.status_code == 200 and response.json["outcome"] == "new"
    row = update_store.get("3001")
    assert row["new_count"] == 1 and row["last_success_at"]
    assert len(checks.snapshot()["update_check_log"]) == 1
    assert checks.db.get_cached_album_detail("3001") is None
    assert checks.clients[0].root.closed


@pytest.mark.parametrize("fail", [False, True], ids=["late-success", "late-failure"])
@pytest.mark.parametrize("trigger", ["manual", "auto"])
def test_stop_retains_real_request_and_discards_late_results(checks, fail, trigger):
    checker = checks.checker
    checks.make_album()
    entered, release = checks.block_fetch(fail=fail)
    event = checker._stop_event
    task = checks.launch(lambda: checker.check_album("3001", trigger))
    assert entered.wait(1)
    before = checks.snapshot()  # includes begin_check's allowed lease
    root = checks.clients[0].root
    assert before["update_check_log"] == []
    begin = time.monotonic()
    assert checker.stop(timeout=0.02) is False
    assert time.monotonic() - begin < 0.4
    assert task.thread.is_alive() and root.in_request and not root.closed
    assert checker.runtime_status()["active_checks"] == 1
    assert event.is_set()
    checker.start()
    assert checker._stop_event is event and not checker._armed
    release.set()
    task.join()
    assert isinstance(task.error, checker.CheckStopped) and task.result is None
    assert root.closed and root.close_thread == task.thread.ident
    assert checks.snapshot() == before  # zero success/failure/log/cache publication
    # Finishing the old request alone does not revive a failed stop generation.
    checker.start()
    assert checker._stop_event is event and not checker._armed
    assert checker.stop(timeout=1)
    checker.start()
    assert checker._stop_event is not event and not checker._stop_event.is_set()
    assert checker.stop(timeout=1)


@pytest.mark.parametrize("fail", [False, True])
def test_stop_between_fetch_and_publication_discards_result(checks, monkeypatch, fail):
    checker = checks.checker
    checks.make_album()
    entered, release = checks.gate(), checks.gate()
    original = checker._begin_publication

    def publication(event):
        entered.set()
        assert release.wait(3)
        original(event)

    if fail:
        checks.answer = lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("offline"))
    monkeypatch.setattr(checker, "_begin_publication", publication)
    task = checks.launch(lambda: checker.check_album("3001"))
    assert entered.wait(1)
    assert checks.clients[0].root.closed
    before = checks.snapshot()
    checker.request_stop()
    assert not checker.stop(timeout=0.01)
    release.set()
    task.join()
    assert isinstance(task.error, checker.CheckStopped)
    assert checks.snapshot() == before


def test_waiting_for_check_lock_responds_to_stop_without_writing_lease(checks):
    checker = checks.checker
    checks.make_album()
    before = checks.snapshot()
    with checker._check_lock:
        task = checks.launch(lambda: checker.check_album("3001", lock_wait=20))
        checks.active(1)
        begin = time.monotonic()
        assert checker.stop(timeout=0.5)
        assert time.monotonic() - begin < 0.4
        task.join()
        assert isinstance(task.error, checker.CheckStopped)
    assert checks.clients == [] and checks.snapshot() == before


def test_concurrent_manual_waiters_are_owned_and_never_fetch_after_stop(checks):
    checker = checks.checker
    for aid in range(3001, 3009):
        checks.make_album(str(aid))
    entered, release = checks.block_fetch()
    first = checks.launch(lambda: checker.check_album("3001"))
    assert entered.wait(1)
    waiters = [checks.launch(lambda aid=str(aid): checker.check_album(aid, lock_wait=20))
               for aid in range(3002, 3009)]
    checks.active(8)
    before = checks.snapshot()
    assert not checker.stop(timeout=0.02)
    for task in waiters:
        task.join()
        assert isinstance(task.error, checker.CheckStopped)
    checks.active(1)
    assert len(checks.clients) == 1 and len(checks.clients[0].root.calls) == 1
    release.set()
    first.join()
    assert isinstance(first.error, checker.CheckStopped)
    assert checker.stop(timeout=1) and checks.snapshot() == before


def test_stop_waits_for_background_and_manual_work_in_one_generation(checks, monkeypatch):
    from core import update_store
    checker = checks.checker
    checks.make_album()
    checks.make_album("3002")
    update_store.postpone("3001", checker._now() - timedelta(seconds=1))
    entered, release = checks.block_fetch()
    monkeypatch.setattr(checker, "STARTUP_DELAY", 0)
    checker.start()
    loop_thread = checker._thread
    assert entered.wait(1)
    manual = checks.launch(lambda: checker.check_album("3002", lock_wait=20))
    checks.active(2)
    before = checks.snapshot()
    stopper = checks.launch(lambda: checker.stop(timeout=1))
    assert not stopper.done.wait(0.03)
    assert loop_thread.is_alive() and checks.clients[0].root.in_request
    release.set()
    stopper.join()
    manual.join()
    assert stopper.error is None and stopper.result is True
    assert isinstance(manual.error, checker.CheckStopped)
    assert not loop_thread.is_alive() and checker._thread is None
    assert checks.snapshot() == before


def test_stop_does_not_report_safe_while_loop_is_still_alive(checks, monkeypatch):
    checker = checks.checker
    entered, release = checks.gate(), checks.gate()

    def loop(event):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(checker, "_loop", loop)
    checker.start()
    assert entered.wait(1)
    thread, event = checker._thread, checker._stop_event
    assert not checker.stop(timeout=0.01)
    assert checker.runtime_status()["active_checks"] == 0
    checker.start()
    assert checker._thread is thread and checker._stop_event is event
    release.set()
    thread.join(1)
    assert checker.stop(timeout=1)


def test_client_cleanup_remains_owned_until_it_really_finishes(checks):
    checker = checks.checker
    checks.make_album()
    entered, release = checks.gate(), checks.gate()

    def close():
        entered.set()
        assert release.wait(3)

    checks.on_close = close
    task = checks.launch(lambda: checker.check_album("3001"))
    assert entered.wait(1)
    before = checks.snapshot()
    assert not checks.clients[0].root.in_request
    assert not checker.stop(timeout=0.01)
    assert checker.runtime_status()["active_checks"] == 1
    release.set()
    task.join()
    assert isinstance(task.error, checker.CheckStopped)
    assert checks.clients[0].root.closed and checks.snapshot() == before


@pytest.mark.parametrize("raises", [False, True], ids=["returns-false", "raises"])
def test_failed_close_is_retained_and_retried_only_by_owned_cleanup_thread(checks, monkeypatch, raises):
    from core import jm_service
    checker = checks.checker
    checks.make_album()
    can_close = checks.gate()
    original, calls = jm_service.close_client, []

    def close(client):
        calls.append((threading.current_thread().name, threading.get_ident()))
        if can_close.is_set():
            return original(client)
        if raises:
            raise RuntimeError("controlled close failure")
        return False

    monkeypatch.setattr(jm_service, "close_client", close)
    task = checks.launch(lambda: checker.check_album("3001"))
    task.join()
    assert task.error is None
    assert checker.runtime_status()["active_checks"] == 0
    assert checker.runtime_status()["pending_clients"] == 1
    assert not checks.clients[0].root.closed
    assert checker.stop(timeout=0.5) is False
    assert checker.runtime_status()["pending_clients"] == 1
    event = checker._stop_event
    checker.start()
    assert checker._stop_event is event and not checker._armed
    can_close.set()
    assert checker.stop(timeout=1)
    assert calls[0][1] == task.thread.ident
    assert all(name == "update-check-cleanup" for name, _ in calls[1:])
    assert all(ident != threading.get_ident() for _, ident in calls)
    assert checks.clients[0].root.closed
    assert checker.runtime_status()["pending_clients"] == 0
    assert checker._cleanup_thread is None


@pytest.mark.parametrize("raises", [False, True])
def test_blocked_cleanup_retry_consumes_only_stop_budget_and_remains_owned(checks, monkeypatch, raises):
    from core import jm_service
    checker = checks.checker
    checks.make_album()
    entered, release = checks.gate(), checks.gate()
    original = jm_service.close_client

    def close(client):
        if threading.current_thread().name == "update-check-cleanup":
            entered.set()
            assert release.wait(3)
            return original(client)
        if raises:
            raise RuntimeError("controlled initial close failure")
        return False

    monkeypatch.setattr(jm_service, "close_client", close)
    task = checks.launch(lambda: checker.check_album("3001"))
    task.join()
    assert task.error is None
    begin = time.monotonic()
    assert checker.stop(timeout=0.02) is False
    assert time.monotonic() - begin < 0.4
    assert entered.wait(1)
    cleanup = checker._cleanup_thread
    assert cleanup.is_alive() and not checks.clients[0].root.closed
    assert checker.runtime_status()["cleanup_running"]
    assert checker.runtime_status()["active_checks"] == 0
    checker.start()
    assert checker._cleanup_thread is cleanup and not checker._armed
    release.set()
    assert checker.stop(timeout=1)
    assert not cleanup.is_alive() and checks.clients[0].root.closed


@pytest.mark.parametrize("late_failure", [False, True])
def test_late_result_and_failed_close_retain_only_cleanup_not_publication(checks, monkeypatch, late_failure):
    from core import jm_service
    checker = checks.checker
    checks.make_album()
    entered, release = checks.block_fetch(fail=late_failure)
    can_close = checks.gate()
    original = jm_service.close_client
    monkeypatch.setattr(jm_service, "close_client", lambda client: original(client) if can_close.is_set() else False)
    task = checks.launch(lambda: checker.check_album("3001"))
    assert entered.wait(1)
    before = checks.snapshot()
    assert checker.stop(timeout=0.01) is False
    release.set()
    task.join()
    assert isinstance(task.error, checker.CheckStopped)
    assert checker.runtime_status()["active_checks"] == 0
    assert checker.stop(timeout=0.5) is False
    assert checker.runtime_status()["pending_clients"] == 1
    assert checks.snapshot() == before
    can_close.set()
    assert checker.stop(timeout=1)
    assert checks.snapshot() == before and checks.clients[0].root.closed


def test_cleanup_thread_start_failure_retains_client_for_later_stop(checks, monkeypatch):
    from core import jm_service
    checker = checks.checker
    checks.make_album()
    can_close = checks.gate()
    original = jm_service.close_client
    monkeypatch.setattr(jm_service, "close_client", lambda client: original(client) if can_close.is_set() else False)
    checker.check_album("3001")
    real_thread = threading.Thread

    def broken_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        thread.start = lambda: (_ for _ in ()).throw(RuntimeError("controlled thread failure"))
        return thread

    with monkeypatch.context() as patch:
        patch.setattr(checker, "threading", SimpleNamespace(Thread=broken_thread, current_thread=threading.current_thread))
        assert checker.stop(timeout=0.01) is False
        assert checker.runtime_status()["pending_clients"] == 1
    can_close.set()
    assert checker.stop(timeout=1) and checks.clients[0].root.closed


@pytest.mark.parametrize("fail", [False, True])
def test_publication_already_admitted_is_irrevocable_and_must_be_drained(checks, monkeypatch, fail):
    from core import update_store
    checker = checks.checker
    checks.make_album()
    entered, release = checks.gate(), checks.gate()
    name = "record_failure" if fail else "record_success"
    original = getattr(update_store, name)

    def record(*args, **kwargs):
        # The publication permit precedes this call. Even a not-yet-started SQL
        # transaction here is now irrevocable; no stop thread closes its handle.
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    if fail:
        checks.answer = lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("offline"))
    monkeypatch.setattr(update_store, name, record)
    task = checks.launch(lambda: checker.check_album("3001"))
    assert entered.wait(1)
    begin = time.monotonic()
    checker.request_stop()
    assert time.monotonic() - begin < 0.2
    assert not checker.stop(timeout=0.01)
    release.set()
    task.join()
    assert task.error is None and task.result["outcome"] == ("failed" if fail else "new")
    assert len(checks.snapshot()["update_check_log"]) == 1
    assert (checks.db.get_cached_album_detail("3001") is not None) is fail
    assert checker.stop(timeout=1)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("late_failure", [False, True])
def test_one_45_second_budget_and_remaining_curl_timeout_across_domains(checks, monkeypatch, late_failure):
    from core import transfer
    from curl_cffi import CurlOpt
    checker, clock = checks.checker, Clock()
    monkeypatch.setattr(checker, "_mono", clock)
    monkeypatch.setattr(transfer, "_clock", clock)
    monkeypatch.setattr(checker, "_cursor", 2)
    attempts = []

    def answer(*args, **kwargs):
        attempts.append(1)
        clock.advance(19 if len(attempts) < 3 else 7)
        if len(attempts) < 3 or late_failure:
            raise ConnectionError("offline")
        return checks.album()

    checks.answer = answer
    with pytest.raises(checker.CheckFailed) as failure:
        checker.fetch_upstream_episodes("3001")
    assert (failure.value.kind, failure.value.requests) == ("timeout", 3)
    fake = checks.clients[0]
    assert fake.selected == [[checks.domains[i]] for i in (2, 3, 0)]
    assert [call[2]["timeout"] for call in fake.root.calls] == [20, 20, 7]
    assert [call[3][CurlOpt.TIMEOUT_MS] for call in fake.root.calls] == [20000, 20000, 7000]
    assert all(call[2]["stream"] is False for call in fake.root.calls)
    assert clock.now == 45 and fake.root.closed


def test_multiple_transport_requests_share_each_sdk_call_cap_and_saved_timeout(checks, monkeypatch):
    from core import transfer
    checker, clock = checks.checker, Clock()
    monkeypatch.setattr(checker, "_mono", clock)
    monkeypatch.setattr(transfer, "_clock", clock)
    monkeypatch.setattr(checker, "get_settings", lambda: {"timeout": "7"})
    calls = []

    def answer(*args, **kwargs):
        calls.append(kwargs["timeout"])
        clock.advance(3)
        return checks.album()

    def album_detail(fake, aid):
        fake.root.get("https://one.invalid/first")
        return fake.root.post("https://one.invalid/second", timeout=900)

    checks.answer = answer
    monkeypatch.setattr(Client, "get_album_detail", album_detail)
    episodes, requests = checker.fetch_upstream_episodes("3001")
    assert len(episodes) == 2 and requests == 1
    assert calls == [7, 4] and checks.clients[0].timeout == 7


@pytest.mark.parametrize("error", [TimeoutError("timed out"), RuntimeError("Operation timed out")])
def test_transport_timeout_never_polls_another_domain(checks, error):
    checker = checks.checker
    checks.answer = lambda *a, **kw: (_ for _ in ()).throw(error)
    with pytest.raises(checker.CheckFailed) as failure:
        checker.fetch_upstream_episodes("3001")
    assert (failure.value.kind, failure.value.requests) == ("timeout", 1)
    assert len(checks.clients[0].selected) == 1 and checks.clients[0].root.closed


def test_missing_postman_is_a_configuration_failure_without_budget_bypass(checks, monkeypatch):
    from core import jm_service
    checker = checks.checker
    fake = SimpleNamespace(get_domain_list=lambda: pytest.fail("uncontrolled transport"))
    closed = []
    monkeypatch.setattr(jm_service, "new_check_client", lambda timeout: fake)
    monkeypatch.setattr(jm_service, "close_client", lambda client: closed.append(client) or True)
    with pytest.raises(checker.CheckFailed) as failure:
        checker.fetch_upstream_episodes("3001")
    assert failure.value.detail == "TransferConfigurationError" and failure.value.requests == 0
    assert closed == [fake]


@pytest.mark.parametrize("client_type", ["api", "html"])
def test_real_sdk_keeps_its_own_domains_and_one_request_per_domain(checks, monkeypatch, client_type):
    from core import jm_service
    from core.settings import update_settings
    from jmcomic import JmModuleConfig
    checker = checks.checker
    update_settings({"client_type": client_type, "timeout": "30", "retry_times": "5"})
    jm_service.invalidate_option_cache()
    before = jm_service.get_active_client_count()
    real = checks.real_new_client(20)
    expected = (JmModuleConfig.DOMAIN_API_LIST if client_type == "api" else JmModuleConfig.DOMAIN_HTML_LIST)
    assert real.get_domain_list() == expected
    assert real.retry_times == 0 and real.get_cache_dict() is None
    root, calls = real.get_root_postman(), []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        raise ConnectionError("controlled, never sent")

    monkeypatch.setattr(root, "get", request)
    monkeypatch.setattr(root, "post", request)
    monkeypatch.setattr(jm_service, "new_check_client", lambda timeout: real)
    with pytest.raises(checker.CheckFailed) as failure:
        checker.fetch_upstream_episodes("3001")
    nodes = dict.fromkeys(domain for domain in expected if domain)
    assert failure.value.kind == "network" and failure.value.requests == len(nodes)
    assert len(calls) == len(nodes)
    assert all(domain in url for domain, (url, _) in zip(nodes, calls))
    assert all(0 < kwargs["timeout"] <= 20 for _, kwargs in calls)
    assert jm_service.get_active_client_count() == before


def runtime_services(events, updates):
    def service(name):
        return SimpleNamespace(start=lambda: events.append("start:" + name),
                               stop=lambda timeout: events.append("stop:" + name) or True,
                               request_stop=lambda: events.append("gate:" + name))

    jobs, scheduler = service("jobs"), service("scheduler")
    jobs.recover_interrupted = lambda: 0
    return jobs, scheduler, updates


def test_runtime_closes_every_gate_before_draining_and_keeps_one_budget(checks, monkeypatch):
    from core import runtime as module
    events, clock, budgets = [], Clock(), []
    updates = SimpleNamespace(start=lambda: None, request_stop=lambda: events.append("gate:updates"))
    jobs, scheduler, updates = runtime_services(events, updates)
    extra = SimpleNamespace(start=lambda: None, request_stop=lambda: events.append("gate:extra"))
    for name, component in (("updates", updates), ("scheduler", scheduler), ("jobs", jobs), ("extra", extra)):
        def stop(timeout, name=name):
            assert {"gate:updates", "gate:jobs", "gate:extra"} <= set(events)
            budgets.append((name, timeout))
            clock.advance(min(3, timeout))
            return timeout >= 3
        component.stop = stop
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock))
    runtime = module.ApplicationRuntime(jobs, scheduler, updates, additional_services=(("extra", extra),))
    runtime.start()
    assert runtime.stop(timeout=5) is False
    assert budgets == [("updates", 5), ("scheduler", 2), ("jobs", 0), ("extra", 0)]
    with pytest.raises(RuntimeError):
        runtime.start()


@pytest.mark.parametrize("failure_stage", ["recovery", "scheduler", "updates"])
def test_runtime_start_rollback_drains_preexisting_manual_checks(checks, failure_stage):
    from core.runtime import ApplicationRuntime
    checker = checks.checker
    checks.make_album()
    entered, release = checks.block_fetch()
    task = checks.launch(lambda: checker.check_album("3001"))
    assert entered.wait(1)
    events = []
    jobs, scheduler, _ = runtime_services(events, checker)

    def fail():
        raise RuntimeError("controlled start failure")

    if failure_stage == "recovery":
        jobs.recover_interrupted = fail
    elif failure_stage == "scheduler":
        scheduler.start = fail
    else:
        # An update service start may own resources and then fail.
        original = checker.start
        def failed_start():
            original()
            fail()
        checker_start = pytest.MonkeyPatch()
        checker_start.setattr(checker, "start", failed_start)
    runtime = ApplicationRuntime(jobs, scheduler, checker, shutdown_timeout=0.02)
    before = checks.snapshot()
    try:
        with pytest.raises(RuntimeError, match="controlled start failure"):
            runtime.start()
        assert checker._stop_event.is_set() and checker.runtime_status()["active_checks"] == 1
        assert runtime._state == "stopping" and any(name == "updates" for name, _ in runtime._services)
        with pytest.raises(RuntimeError):
            runtime.start()
        release.set()
        task.join()
        assert isinstance(task.error, checker.CheckStopped)
        assert runtime.stop(timeout=1) and checks.snapshot() == before
    finally:
        if failure_stage == "updates":
            checker_start.undo()


def test_runtime_stop_during_recovery_closes_update_admission_before_lock_wait(checks):
    from core.runtime import ApplicationRuntime
    checker = checks.checker
    entered, release = checks.gate(), checks.gate()
    events = []
    jobs, scheduler, _ = runtime_services(events, checker)

    def recover():
        entered.set()
        assert release.wait(3)
        return 0

    jobs.recover_interrupted = recover
    runtime = ApplicationRuntime(jobs, scheduler, checker)
    starter = checks.launch(runtime.start)
    assert entered.wait(1)
    assert not runtime.stop(timeout=0.01)
    assert checker._stop_event.is_set()
    with pytest.raises(checker.CheckStopped):
        checker.check_album("3001")
    release.set()
    starter.join()
    assert isinstance(starter.error, RuntimeError) and "停机请求" in str(starter.error)
    assert not checker._armed and checker._thread is None
    assert events == ["gate:jobs", "gate:jobs"]
    assert runtime.stop(timeout=1)


def test_runtime_stop_before_start_still_drains_manual_requests(checks):
    from core.runtime import ApplicationRuntime
    checker = checks.checker
    checks.make_album()
    entered, release = checks.block_fetch()
    manual = checks.launch(lambda: checker.check_album("3001"))
    assert entered.wait(1)
    runtime = ApplicationRuntime(*runtime_services([], checker))
    assert not runtime.stop(timeout=0.01)
    release.set()
    manual.join()
    assert isinstance(manual.error, checker.CheckStopped)
    assert runtime.stop(timeout=1)
