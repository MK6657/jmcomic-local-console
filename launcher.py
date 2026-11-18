"""Start the local console without killing other programs or blocking log pipes.

A healthy server started from this folder is reused, unless the program files changed after it started;
then an idle, verified server can be cooperatively restarted to load the update.
"""
import argparse
import hashlib
from http.client import HTTPException
import json
import os
import subprocess
import sys
import time
import webbrowser
import uuid
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from core.path_utils import get_data_dir, get_log_dir, get_path_config
from core.process_control import request_shutdown, SHUTDOWN_PROTOCOL
from core.runtime_dependencies import installed_versions
from core.instance_lock import acquire_instance_lock

HOST = "127.0.0.1"
ROOT = Path(__file__).resolve().parent
FLASK_JSON = get_data_dir() / "flask.json"
MAX_WAIT = 30
POLL_INTERVAL = 0.5
# Changes here only take effect after a restart; static files are cache-busted on every page load.
RESTART_GLOBS = ("app.py", "core/*.py", "routes/*.py", "templates/*.html", "requirements.txt")
# launcher.log housekeeping (the app's own app.log/error.log are handled by core/logger.py).
LAUNCHER_LOG = "launcher.log"
LAUNCHER_LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_MAX_AGE_DAYS = 7


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _local_json(port, endpoint, *, limit=8192):
    with build_opener(ProxyHandler({}), _NoRedirect()).open(f"http://{HOST}:{port}{endpoint}", timeout=2) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("本地服务响应过大")
    return json.loads(data)


def _matches_health(health, pid):
    return (health.get("application") == "jmcomic-local-console" and health.get("status") == "ok"
            and type(health.get("pid")) is int and health["pid"] == pid)


def _verify_instance(health, record):
    if not health.get("instance_id"):
        raise RuntimeError("旧服务未提供实例目录身份，已取消操作。请使用原启动窗口；待任务结束后正常退出再升级。")
    if health["instance_id"] != get_path_config().instance_id(record.get("launch_token", "")):
        raise RuntimeError("运行服务与所选实例、存储目录或启动记录不匹配，已取消操作。请使用原启动窗口或原目录配置。")


def get_running_port(expected_pid=None, expected_token=None):
    """Require both our startup record and a matching application/PID response."""
    server = get_running_server(expected_pid, expected_token)
    return server[0] if server else None


def get_running_server(expected_pid=None, expected_token=None):
    """(port, startup record) of our verified running server, or None."""
    try:
        record = json.loads(FLASK_JSON.read_text(encoding="utf-8"))
        port, pid = int(record["port"]), int(record["pid"])
        if port not in (5000, 5001, 5002, 5003) or pid <= 0:
            return None
        if expected_pid is not None and pid != expected_pid:
            return None
        if expected_token is not None and record.get("launch_token") != expected_token:
            return None
        health = _local_json(port, "/api/system/health")
        if _matches_health(health, pid):
            _verify_instance(health, record)
            return port, record
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def code_fingerprint(root=ROOT):
    """Fingerprint recorded by the service, using its own interpreter's installed runtime versions."""
    root = Path(root)
    digest = hashlib.sha256()
    if getattr(sys, "frozen", False):
        # Frozen Python modules live in the archive, not core/*.py. Include the
        # executable and build manifest so backend/dependency updates count too.
        for path in (Path(sys.executable), root / "release.json"):
            digest.update(path.read_bytes())
        return digest.hexdigest()
    for pattern in RESTART_GLOBS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            data = path.read_bytes()
            digest.update(f"{path.relative_to(root).as_posix()}\0{len(data)}\0".encode("utf-8"))
            digest.update(data)
            if path == root / "requirements.txt":
                digest.update(json.dumps(installed_versions(data.decode("utf-8-sig"))).encode("utf-8"))
    return digest.hexdigest()


def code_updated_since(timestamp, root=ROOT):
    """Fallback for a server that recorded no fingerprint: True when a file's modification or creation time
    is later than the server start. Misses files copied over existing ones with an older modification time."""
    for pattern in RESTART_GLOBS:
        for path in root.glob(pattern):
            info = path.stat()
            # A newly created file gets the copy time as its creation time (not so when overwriting a file).
            if max(info.st_mtime, getattr(info, "st_birthtime", 0)) > timestamp:
                return True
    return False


def server_outdated(record, root=ROOT):
    """True when the running server was started from different program files than the ones on disk now."""
    fingerprint = record.get("code_fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        executable = _server_python(root)
        if not getattr(sys, "frozen", False) and executable != Path(sys.executable):
            probe = subprocess.run([str(executable), "-c", "from launcher import code_fingerprint; print(code_fingerprint())"],
                                   cwd=root, capture_output=True, timeout=15, check=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            current = probe.stdout.strip().decode("ascii")
            if len(current) != 64 or any(char not in "0123456789abcdef" for char in current):
                raise ValueError("无法核实服务解释器的运行依赖")
        else:
            current = code_fingerprint(root)
        return fingerprint != current
    try:
        started = float(record.get("timestamp", 0))
    except (TypeError, ValueError):
        started = 0  # unknown start time: treat as outdated
    return code_updated_since(started, root)


def flush_server_logs(port, timeout=3):
    """Ask the server to write pending log dedup summaries. This is best effort."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        return False  # port unknown (the server never became ready)
    try:
        opener = build_opener(ProxyHandler({}))  # never through a system proxy
        request = Request(f"http://{HOST}:{port}/api/system/logs/flush", data=b"", method="POST")
        with opener.open(request, timeout=timeout) as response:
            response.read(4096)
        return True
    except Exception:
        return False


def process_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return False
        raise ctypes.WinError(error)
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result not in (0, 0x102):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0x102
    finally:
        kernel32.CloseHandle(handle)


def stop_outdated_server(pid, timeout=20, port=None, record=None, *, only_if_idle=False):
    """Notify a verified service and wait for actual exit; never force an upgrade."""
    verified = get_running_server(expected_pid=pid)
    if verified is None or verified[0] != port:
        if not process_alive(pid):
            return
        raise RuntimeError("服务身份无法再次核实，已保留进程，请检查后重试")
    current = verified[1]
    if record is not None and record != current:
        raise RuntimeError("运行服务的身份已变化，已取消重启")
    if only_if_idle:
        reason = _restart_blocker(port, current)
        if reason:
            return reason
    if os.name == "nt":
        if current.get("shutdown_protocol") != SHUTDOWN_PROTOCOL:
            raise RuntimeError("旧服务不支持协作停机，请在原启动窗口正常关闭，再启动新版本")
        if not request_shutdown(current.get("shutdown_token")):
            raise RuntimeError("未找到该服务的停机通道，已保留进程")
    else:
        os.kill(pid, 15)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"服务仍在退出（PID {pid}），已取消重启；请等待在途任务结束后重试")


def _day(timestamp):
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def rotate_launcher_log(log_dir, now=None):
    """Before a launch: archive launcher.log as launcher.log.YYYY-MM-DD[.N] when it is larger than
    LAUNCHER_LOG_MAX_BYTES or was last written on an earlier day, and delete launcher.log.* older than
    LOG_MAX_AGE_DAYS. A file still held open by a running server cannot be renamed/deleted on Windows;
    that is skipped and retried at the next launch."""
    log_dir = Path(log_dir)
    now = time.time() if now is None else now
    active = log_dir / LAUNCHER_LOG
    try:
        info = active.stat()
    except OSError:
        info = None
    if info is not None and info.st_size > 0 and (
            info.st_size > LAUNCHER_LOG_MAX_BYTES or _day(info.st_mtime) != _day(now)):
        target = log_dir / f"{LAUNCHER_LOG}.{_day(info.st_mtime)}"
        number = 0
        while target.exists():
            number += 1
            target = log_dir / f"{LAUNCHER_LOG}.{_day(info.st_mtime)}.{number}"
        try:
            os.rename(active, target)
        except OSError:
            pass
    cutoff = now - LOG_MAX_AGE_DAYS * 86400
    for path in log_dir.glob(f"{LAUNCHER_LOG}.*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _server_python(root):
    venv_python = Path(root) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return venv_python if not getattr(sys, "frozen", False) and venv_python.is_file() else Path(sys.executable)


def _setup_needed(root):
    requirements = root / "requirements.txt"
    stamp = root / ".venv" / ".req.sha256"
    try:
        installed = stamp.read_text(encoding="ascii")
    except (FileNotFoundError, UnicodeError):
        return True
    return (not (stamp.parent / "Scripts/python.exe").is_file()
            or installed != hashlib.sha256(requirements.read_bytes()).hexdigest())


def _prepare_source(root):
    """Exclude a starting, draining, or unverified instance throughout venv mutation."""
    lease = acquire_instance_lock()
    try:
        if not _setup_needed(root):
            return
        venv = root / ".venv"
        executable = venv / "Scripts/python.exe"
        requirements = root / "requirements.txt"
        digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
        stamp = venv / ".req.sha256"
        stamp.unlink(missing_ok=True)
        if not executable.is_file():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], cwd=root, check=True)
        subprocess.run([str(executable), "-m", "pip", "install", "-r", str(requirements)], cwd=root, check=True)
        subprocess.run([str(executable), "-m", "pip", "check"], cwd=root, check=True)
        if hashlib.sha256(requirements.read_bytes()).hexdigest() != digest:
            raise RuntimeError("依赖文件在安装期间已变化，请重新启动")
        stamp.write_text(digest, encoding="ascii")
    finally:
        lease.close()


def launch_flask(root=None):
    """Launch source Python or this portable executable; never undrained PIPEs."""
    paths = get_path_config(default_home=Path(root) if root is not None else None)
    root = ROOT if root is None else Path(root)
    frozen = getattr(sys, "frozen", False)
    if frozen:
        executable = sys.executable
        command = [executable, "serve"]
        working_directory = str(Path(executable).parent)
    else:
        executable = str(_server_python(root))
        command = [executable, str(root / "app.py")]
        working_directory = str(root)
    log_dir = paths.logs
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        rotate_launcher_log(log_dir)
    except Exception:
        pass  # housekeeping must never block a launch
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    token = uuid.uuid4().hex
    environment = dict(os.environ, JMCONSOLE_LAUNCH_TOKEN=token)
    environment.update(paths.as_environment())
    if frozen:
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    # Child output lands in launcher.log: keep it UTF-8 (not the console code page) so it stays readable.
    environment["PYTHONIOENCODING"] = "utf-8:backslashreplace"
    with (log_dir / LAUNCHER_LOG).open("ab") as output:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        output.write(f"[{stamp}] [INFO] [launcher] starting server: {executable} app.py\n".encode("utf-8"))
        output.flush()
        proc = subprocess.Popen(
            command, cwd=working_directory,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            env=environment, **kwargs,
        )
    proc.jm_launch_token = token
    return proc


def cleanup(proc):
    """Drain our own child without interpreting a missing event as safe to kill."""
    if proc.poll() is None:
        token = getattr(proc, "jm_launch_token", None)
        notified = isinstance(token, str) and request_shutdown(token)
        if notified:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                raise RuntimeError("服务尚未完成退出，进程已保留；请等待在途任务结束") from None
            if proc.returncode:
                raise RuntimeError("服务退出未完成全部清理，请检查日志后再启动")
            return
        # The event may already have closed while Python is still waiting for
        # an in-flight SDK future. Absence never proves startup did no work.
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            raise RuntimeError("停机通道不可用，进程仍在运行，已保留；请检查日志后重试") from None
        if proc.returncode:
            raise RuntimeError("服务启动或退出失败，请检查日志")


def wait_until_ready(proc, timeout=MAX_WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server exited during startup. See {get_log_dir() / LAUNCHER_LOG}. "
                               "若其他实例仍在运行，请回到原启动窗口，或用相同目录配置运行 JMComic.exe attach。")
        # Windows venv python.exe may be a redirector with a different server PID.
        # The per-launch token follows its child while marker/health PIDs still match.
        port = get_running_port(expected_token=proc.jm_launch_token)
        if port is not None:
            return port
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Server startup timed out. See {get_log_dir() / LAUNCHER_LOG}.")


def _restart_blocker(port, record):
    """Unknown/legacy responses cannot establish idleness; never use the upstream diagnostic."""
    try:
        health = _local_json(port, "/api/system/health?activity=1")
        if not _matches_health(health, int(record["pid"])):
            raise RuntimeError("运行服务的身份已变化，已取消重启")
        _verify_instance(health, record)
        activity = [health["jobs"][state] for state in ("queued", "running", "paused", "executors")]
        activity.append(health["jm_active_clients"])
        if all(type(value) is int and value >= 0 for value in activity):
            if any(activity):
                return "已暂缓升级：排队 {}、运行 {}、暂停 {}、下载执行器 {}、在用 SDK 客户端 {}。".format(*activity)
            return None
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        pass
    return "无法确认任务是否空闲，已暂缓升级。"


def _attach(port, record, quiet, reason=None):
    if get_running_server(expected_pid=int(record["pid"])) != (port, record):
        raise RuntimeError("运行服务的身份已变化，已取消打开页面；请重试")
    message = ((reason + " 请在任务结束后重新启动，或在原窗口正常退出。\n") if reason else "")
    message += (f"已打开既有实例（PID {record['pid']}）：{get_path_config().home}。\n"
                "沿用原服务的目录设置；退出本窗口不会停止该服务。")
    webbrowser.open(f"http://{HOST}:{port}")
    if not quiet:
        print(message)
    elif reason and os.name == "nt" and sys.stdout is None:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "JMComic：继续使用已有服务", 0x40)
    return 0


def main(argv=None, quiet=False):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--wait", "-w", action="store_true", help="按 Enter 正常停止本窗口启动的服务")
    group.add_argument("--attach", action="store_true", help="仅打开经核实的既有实例，不启动或重启服务")
    parser.add_argument("--source-setup", action="store_true", help="在独占实例锁内安装源码版依赖")
    args = parser.parse_args(argv)
    if args.source_setup and (args.attach or os.name != "nt" or getattr(sys, "frozen", False)):
        parser.error("--source-setup 只适用于 Windows 源码启动")
    needs_setup = args.source_setup and _setup_needed(ROOT)
    if not args.attach:
        from core.database import _existing_schema
        _existing_schema()
    server = get_running_server()
    if server is not None:
        port, record = server
        if args.attach:
            return _attach(port, record, quiet)
        try:
            outdated = needs_setup or server_outdated(record)
        except (OSError, ImportError, ValueError, subprocess.SubprocessError):
            return _attach(port, record, quiet, "无法核实新版本或其运行依赖，已保留旧服务。")
        if not outdated:
            return _attach(port, record, quiet)
        if os.name == "nt" and record.get("shutdown_protocol") != SHUTDOWN_PROTOCOL:
            return _attach(port, record, quiet, "旧服务不支持协作停机，已暂缓升级。")
        if os.name == "nt" and (not record.get("launch_token") or record["launch_token"] != record.get("shutdown_token")):
            return _attach(port, record, quiet, "无法核实停机通道属于本次启动，已暂缓升级。")
        if not quiet:
            print(f"程序或运行依赖已更新，正在检查任务；仅在实例空闲时协作重启（PID {record['pid']}）…")
        reason = stop_outdated_server(int(record["pid"]), port=port, record=record, only_if_idle=True)
        if reason:
            return _attach(port, record, quiet, reason)
    elif args.attach:
        raise RuntimeError("所选实例没有可核实的运行服务；请核对实例目录或回到原启动窗口。")
    if needs_setup:
        _prepare_source(ROOT)
    proc = launch_flask()
    keep_running = False
    try:
        port = wait_until_ready(proc)
        url = f"http://{HOST}:{port}"
        webbrowser.open(url)
        if not quiet:
            print(f"Running: {url} (launcher child PID {proc.pid})")
        if args.wait:
            input("按 Enter 或 Ctrl+C 正常停止本窗口启动的服务并退出… ")
        else:
            keep_running = True
        return 0
    except (KeyboardInterrupt, EOFError):
        return 0
    finally:
        if not keep_running:
            cleanup(proc)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        sys.exit(1)
