"""检查新章节：唯一联网的部分。一次检查 = 向上游要一次这部漫画的章节列表，从不下载任何内容。

  - 每次检查新建一个不带缓存、不重试的 client（jm_service.new_check_client），只调用一次取专辑详情
    （一个 HTTP 请求）；从上次答复的域名开始，每个域名最多问一次，第一个答复就停（通常 1 个请求，最多 5 个）。
    每个请求最多 20 秒，45 秒后不再换下一个域名。
  - 从不建下载任务、不写文件、不碰收藏 / 元数据 / 标签；除了 album_update_checks / update_check_log 两张表，
    唯一的副作用是确认了新章节时删掉这一部漫画的详情缓存，章节表才会显示新章节。
  - 只查本地有已下载内容（散图或能读的 CBZ / ZIP，包括只下载了部分章节的）的漫画。
  - 自动检查和手动“立即检查”共用一把锁：同一时间只查一部。

后台线程只由 app.main() 启动（start()）；create_app()、保存设置、测试都不会启动它。循环每一轮重新读
auto_update_check 设置，所以设置页不需要通知这里。节奏见下面的常量（故意不做成设置：用户和导入的设置文件
都不能让它频繁请求上游）。
"""
import random
import threading
import time
from datetime import datetime, timedelta

from jmcomic import JmcomicException, MissingAlbumPhotoException

from . import jm_service
from . import local_availability
from . import update_store
from .logger import clear_request_id, log, set_request_id
from .settings import get_settings

# ── 节奏 ──
STARTUP_DELAY = 300          # 程序启动后先等 5 分钟……
STARTUP_JITTER = 120         # ……再随机加 0–2 分钟
GAP = 120                    # 任意两次检查（自动或手动）之间至少隔 2 分钟……
GAP_JITTER = 60              # ……再随机加 0–1 分钟；从上一次检查结束算起，用单调时钟
DAILY = update_store.DAILY   # 成功后 23–25 小时再查这一部
LEASE = 3600                 # 发请求前先把下一次自动检查推到 1 小时后（租约）
BACKOFF = (1800, 3600, 7200, 14400, 28800, 57600, 86400)  # 连续失败：30 分钟 → 24 小时（各种原因一样）
BACKOFF_JITTER = (0.9, 1.1)
NET_PAUSE = (900, 1800, 3600, 7200, 14400, 21600)  # 连续 k ≥ 2 次自动检查连不上服务器：暂停 15 分钟 → 6 小时
DEFER_RUNNING = 120          # 有下载在进行：2 分钟后再看
BUSY_RETRY = 30              # 锁被手动检查占着：30 秒后再看
IDLE = (60, 600)             # 没有到期的：等到最早的下一次（至少 1 分钟、最多 10 分钟后再看）
DISABLED_POLL = 60           # 自动检查关着：每分钟看一次设置
MANUAL_LOCK_WAIT = 20        # 手动检查最多等 20 秒拿锁，拿不到就说“正在检查别的漫画”
MANUAL_THROTTLE = 60         # 同一部漫画 60 秒内成功检查过：手动检查直接返回上次的结果
REQ_TIMEOUT_CAP = 20         # 每个请求最多 20 秒（设置里的超时更短时用设置的）
CHECK_BUDGET = 45            # 一次检查 45 秒后不再换下一个域名
NOT_READABLE_POSTPONE = 86400  # 到期但本地没有能读的内容：一天后再看（不发请求）
CLOCK_GUARD = 8 * 86400      # next_check_at 比现在晚 8 天以上（系统时间被往回调过）也算到期
DUE_BATCH = 50
LOOP_MAX_WAIT = 600


class CheckFailed(Exception):
    """这次没检查成功（kind：network / timeout / not_found / upstream_error；detail 只是异常类名）"""

    def __init__(self, kind: str, detail: str = "", requests: int = 0):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = str(detail or "")[:120]
        self.requests = requests


class NotTarget(Exception):
    """本地没有能读的已下载内容：不检查（不发请求）"""


class Busy(Exception):
    """正在检查别的漫画（锁被占着）"""


# ── 测试可替换的时钟与随机数 ──
def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


_mono = time.monotonic
_uniform = random.uniform

# ── 运行状态（只在内存里；重启后租约和 next_check_at 保证不会马上重查）──
_check_lock = threading.Lock()   # 自动与手动检查共用：同一时间只查一部
_state_lock = threading.Lock()   # 保护下面这些运行状态
_thread_lock = threading.Lock()  # start / stop 串行化
_thread: threading.Thread | None = None
_stop_event = threading.Event()
_armed = False                   # 只有 app.main() 调用 start() 后才为 True
_phase = "starting"
_resume_at: datetime | None = None
_current: dict | None = None     # 正在检查的漫画 {album_id, title, started_at}
_last_end: float | None = None   # 上一次检查结束时的单调时间
_gap = 0.0
_pause_until = 0.0               # 连不上服务器时自动检查暂停到（单调时间）
_pause_wall: datetime | None = None
_net_failures = 0                # 连续几次自动检查连不上服务器
_cursor = 0                      # 上次答复的域名下标


def _iso(value: datetime | None) -> str | None:
    return value.replace(microsecond=0).isoformat() if value is not None else None


def _parse(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def _set_phase(phase: str, resume: datetime | None = None) -> None:
    global _phase, _resume_at
    with _state_lock:
        _phase = phase
        _resume_at = resume


def _backoff(fail_count: int) -> float:
    return BACKOFF[min(max(fail_count, 1) - 1, len(BACKOFF) - 1)] * _uniform(*BACKOFF_JITTER)


def _request_timeout() -> int:
    try:
        value = int(get_settings().get("timeout", REQ_TIMEOUT_CAP))
    except (TypeError, ValueError):
        value = REQ_TIMEOUT_CAP
    return max(1, min(value, REQ_TIMEOUT_CAP))


def _classify(error: Exception) -> str:
    """一个域名的错误：not_found 上游找不到这部漫画 / upstream_error 上游答复了但看不懂 /
    timeout 超时 / network 其他（连不上、代理错误……）"""
    if isinstance(error, MissingAlbumPhotoException):
        return "not_found"
    if isinstance(error, JmcomicException):
        return "upstream_error"
    text = f"{type(error).__name__} {error}".lower()
    return "timeout" if "timed out" in text or "timeout" in text else "network"


def _final_kind(kinds: list[str]) -> str:
    """几个域名都没成功时的结论：not_found > upstream_error > timeout（全部超时）> network"""
    if "not_found" in kinds:
        return "not_found"
    if "upstream_error" in kinds:
        return "upstream_error"
    if kinds and all(kind == "timeout" for kind in kinds):
        return "timeout"
    return "network"


def _validate(album, album_id: str):
    """上游答复的必须是这部漫画（没有 cookie 时 API 可能返回别的占位本子），章节列表要看得懂；
    否则算 upstream_error，绝不会变成“有新章节”。"""
    if str(getattr(album, "album_id", "")) != album_id:
        raise ValueError("上游返回的不是这部漫画")
    return update_store.episodes_of(album)


def fetch_upstream_episodes(album_id: str):
    """向上游要一次这部漫画的章节列表 → ([(photo_id, index, title)], 请求数)；没成功时抛 CheckFailed。

    从上次答复的域名开始，每个域名最多问一次，第一个答复就停；45 秒后不再换下一个域名。
    只有一个上游调用（取专辑详情，一个 HTTP 请求），不取章节详情、图片、scramble，
    不用共享 client、详情缓存或搜索结果。在调用方的线程里执行，由 curl 的总超时限时。"""
    global _cursor
    album_id = str(album_id)
    client = jm_service.new_check_client(_request_timeout())
    kinds: list[str] = []
    detail, requests = "", 0
    started = _mono()
    try:
        domains = [domain for domain in (client.get_domain_list() or []) if domain]
        if not domains:
            raise CheckFailed("network", "NoDomain", 0)
        first = _cursor % len(domains)
        for n, index in enumerate(list(range(first, len(domains))) + list(range(first))):
            if n and _mono() - started > CHECK_BUDGET:
                break
            client.set_domain_list([domains[index]])
            requests += 1
            try:
                album = client.get_album_detail(album_id)
            except MissingAlbumPhotoException as e:
                raise CheckFailed("not_found", type(e).__name__, requests) from None
            except Exception as e:
                kinds.append(_classify(e))
                detail = type(e).__name__
                continue
            try:
                episodes = _validate(album, album_id)
            except Exception as e:
                kinds.append("upstream_error")
                detail = f"Invalid{type(e).__name__}"
                continue
            _cursor = index
            return episodes, requests
        raise CheckFailed(_final_kind(kinds), detail, requests)
    finally:
        jm_service.close_client(client)


def check_album(album_id: str, trigger: str = "manual", lock_wait: float | None = None) -> dict:
    """检查一部漫画（自动和手动共用的唯一入口）。上游出错不抛异常，记为失败。

    本地没有能读的内容 → NotTarget（不发请求）；lock_wait 秒内拿不到锁 → Busy。
    手动检查：等锁期间这部漫画已经被查过 → 'coalesced'；60 秒内成功检查过 → 'throttled'（都不发请求）。
    返回 {outcome: baseline / no_update / new / changed / failed / throttled / coalesced, ...}。"""
    global _current, _last_end, _gap, _net_failures, _pause_until, _pause_wall
    album_id = str(album_id)
    if not local_availability.is_readable(album_id):
        raise NotTarget(album_id)
    requested = _now()
    wait = MANUAL_LOCK_WAIT if lock_wait is None else lock_wait
    lock = _check_lock
    if not (lock.acquire(timeout=wait) if wait > 0 else lock.acquire(blocking=False)):
        raise Busy(album_id)
    fetched = own_request_id = False
    try:
        if trigger == "manual":
            row = update_store.get(album_id)
            attempted = _parse(row.get("last_attempt_at")) if row else None
            succeeded = _parse(row.get("last_success_at")) if row else None
            skipped = None
            now = _now()
            # 只认不在“将来”的时间：系统时钟往回调过时，记下的时间可能在将来，不能因此一直跳过检查
            if attempted is not None and requested <= attempted <= now:
                skipped = "coalesced"   # 等锁的时候刚查过这一部
            elif (succeeded is not None and succeeded <= now
                  and 0 <= (requested - succeeded).total_seconds() < MANUAL_THROTTLE):
                skipped = "throttled"
            if skipped:
                log.info(f"检查新章节 album_id={album_id} trigger={trigger} outcome={skipped} requests=0")
                return {"outcome": skipped, "newly_confirmed": []}
        if not local_availability.is_readable(album_id):
            raise NotTarget(album_id)
        started = _now()
        update_store.begin_check(album_id, trigger, started, LEASE)
        fetched = True   # 租约已写：从这里起算一次检查（结束时记下间隔、清掉 _current）
        current = {"album_id": album_id, "title": update_store.title_of(album_id), "started_at": _iso(started)}
        with _state_lock:
            _current = current
        if trigger == "auto":
            set_request_id()  # 后台检查自己一个 request_id；手动检查沿用这次 HTTP 请求的
            own_request_id = True
        t0 = _mono()
        try:
            episodes, requests = fetch_upstream_episodes(album_id)
        except CheckFailed as failure:
            update_store.record_failure(album_id, failure.kind, failure.detail, trigger, started, _now(),
                                        failure.requests, _backoff)
            if trigger == "auto":
                with _state_lock:
                    if failure.kind in ("network", "timeout"):
                        _net_failures += 1
                        if _net_failures >= 2:
                            pause = NET_PAUSE[min(_net_failures - 2, len(NET_PAUSE) - 1)]
                            _pause_until = _mono() + pause
                            _pause_wall = _now() + timedelta(seconds=pause)
                    else:
                        _net_failures = 0   # 上游答复了：网络是通的
            log.warning(f"检查新章节 album_id={album_id} trigger={trigger} outcome=failed kind={failure.kind} "
                        f"error={failure.detail} requests={failure.requests} ms={int((_mono() - t0) * 1000)}")
            return {"outcome": "failed", "error_kind": failure.kind, "newly_confirmed": []}
        info = update_store.record_success(album_id, episodes, trigger, started, _now(), requests,
                                           _now() + timedelta(seconds=_uniform(*DAILY)))
        with _state_lock:
            _net_failures = 0
            _pause_until = 0.0
            _pause_wall = None
        if info["newly_confirmed"]:
            try:
                jm_service.forget_album_detail(album_id)  # 详情页下次打开时重新取章节列表，新章节才会出现
            except Exception as e:
                log.warning(f"丢弃详情缓存失败 album_id={album_id} error={e}")
        log.info(f"检查新章节 album_id={album_id} trigger={trigger} outcome={info['result']} "
                 f"upstream={info['upstream_count']} new={info['new_count']} requests={requests} "
                 f"ms={int((_mono() - t0) * 1000)}")
        return {"outcome": info["result"], "newly_confirmed": info["newly_confirmed"]}
    finally:
        if fetched:
            with _state_lock:
                _current = None
                _last_end = _mono()
                _gap = GAP + _uniform(0, GAP_JITTER)
        lock.release()
        if own_request_id:
            clear_request_id()


def _idle_delay(now: datetime) -> float:
    upcoming = update_store.next_due_at()
    if upcoming is None:
        return IDLE[1]
    return min(max((upcoming - now).total_seconds(), IDLE[0]), IDLE[1])


def tick() -> float:
    """后台循环的一轮：最多检查一部漫画，返回下一轮之前要等的秒数。"""
    now = _now()
    if get_settings().get("auto_update_check", "true") != "true":
        _set_phase("off")
        return DISABLED_POLL
    mono = _mono()
    with _state_lock:
        pause_left = _pause_until - mono
        pause_wall = _pause_wall
        gap_left = _last_end + _gap - mono if _last_end is not None else 0
    if pause_left > 0:
        _set_phase("paused", pause_wall)
        return pause_left
    if gap_left > 0:
        _set_phase("idle")
        return gap_left
    if update_store.download_running():   # 只读数据库
        _set_phase("deferred")
        return DEFER_RUNNING
    update_store.register_legacy(now)
    due = update_store.due_candidates(now, now + timedelta(seconds=CLOCK_GUARD), DUE_BATCH)
    target = None
    if due:
        states = local_availability.local_states(due)
        later = now + timedelta(seconds=NOT_READABLE_POSTPONE)
        for album_id in due:
            state = states.get(album_id)
            if state is not None and state.readable:
                target = target or album_id
            else:
                update_store.postpone(album_id, later)   # 本地没有能读的内容：不发请求，一天后再看
    if target is None:
        _set_phase("idle")
        return _idle_delay(now)
    _set_phase("idle")
    try:
        check_album(target, "auto", lock_wait=0)
    except Busy:
        return BUSY_RETRY
    except NotTarget:
        update_store.postpone(target, now + timedelta(seconds=NOT_READABLE_POSTPONE))
        return 5
    with _state_lock:
        return _gap


def _loop(stop_ev: threading.Event) -> None:
    delay = STARTUP_DELAY + _uniform(0, STARTUP_JITTER)
    _set_phase("starting", _now() + timedelta(seconds=delay))
    if stop_ev.wait(delay):
        return
    while not stop_ev.is_set():
        try:
            delay = tick()
        except Exception as e:
            log.error(f"检查新章节调度异常: {e}")
            delay = LOOP_MAX_WAIT
        if stop_ev.wait(max(5, min(delay, LOOP_MAX_WAIT))):
            return


def start() -> None:
    """启动后台检查线程（幂等）。只由 app.main() 调用。"""
    global _armed, _thread, _stop_event
    with _thread_lock:
        _armed = True
        if _thread is not None and _thread.is_alive():
            return
        stop_ev = threading.Event()   # 本线程专属，stop() 只会操作到它
        _stop_event = stop_ev
        _thread = threading.Thread(target=_loop, args=(stop_ev,), daemon=True, name="update-checker")
        _thread.start()
    log.info("检查新章节后台线程已启动（只取章节列表，从不下载）")


def stop() -> None:
    """停止后台检查线程（幂等）。正在进行的一次检查会在自己的时限内结束并释放锁。"""
    global _armed, _thread
    with _thread_lock:
        _armed = False
        _stop_event.set()
        if _thread is not None:
            _thread.join(timeout=5)
            _thread = None
            log.info("检查新章节后台线程已停止")


def is_checking(album_id: str) -> bool:
    with _state_lock:
        return _current is not None and _current["album_id"] == str(album_id)


def paused_until() -> str | None:
    """连不上服务器、自动检查暂停到什么时候（没有暂停时 None）。"""
    with _state_lock:
        return _iso(_pause_wall) if _pause_wall is not None and _mono() < _pause_until else None


def runtime_status() -> dict:
    """{armed, running, phase: starting / idle / checking / deferred / paused / off, resume_at, current}"""
    with _state_lock:
        current = dict(_current) if _current else None
        paused = _pause_wall is not None and _mono() < _pause_until
        if current:
            phase = "checking"
        elif paused and _phase != "off":
            phase = "paused"   # 刚因连不上服务器暂停、后台还没到下一轮时也如实显示
        elif _phase == "paused":
            phase = "idle"     # 暂停已结束（或手动检查成功后解除），后台下一轮就继续
        else:
            phase = _phase
        resume = _pause_wall if phase == "paused" else (_resume_at if phase == "starting" else None)
        running = _thread is not None and _thread.is_alive()
        return {"armed": _armed, "running": running, "phase": phase, "resume_at": _iso(resume), "current": current}


def _reset_for_tests() -> None:
    """测试用：停掉线程、清空运行状态、换一把新锁。"""
    global _check_lock, _phase, _resume_at, _current, _last_end, _gap, _pause_until, _pause_wall
    global _net_failures, _cursor
    stop()
    with _state_lock:
        _check_lock = threading.Lock()
        _phase, _resume_at, _current = "starting", None, None
        _last_end, _gap = None, 0.0
        _pause_until, _pause_wall = 0.0, None
        _net_failures, _cursor = 0, 0
