"""Start the local console without killing other programs or blocking log pipes.

A healthy server started from this folder is reused, unless the program files changed after it started;
then only that verified server is stopped and replaced, so an update takes effect with one start.bat.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import webbrowser
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from core.path_utils import get_data_dir, get_log_dir, get_path_config
from core.process_control import request_shutdown, SHUTDOWN_PROTOCOL

HOST = "127.0.0.1"
ROOT = Path(__file__).resolve().parent
FLASK_JSON = get_data_dir() / "flask.json"
MAX_WAIT = 30
POLL_INTERVAL = 0.5
# Changes here only take effect after a restart; static files are cache-busted on every page load.
RESTART_GLOBS = ("app.py", "core/*.py", "routes/*.py", "templates/*.html")
# launcher.log housekeeping (the app's own app.log/error.log are handled by core/logger.py).
LAUNCHER_LOG = "launcher.log"
LAUNCHER_LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_MAX_AGE_DAYS = 7


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
        # Never send local health checks through a system proxy.
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://{HOST}:{port}/api/system/health", timeout=1) as response:
            health = json.loads(response.read(8192))
        if (health.get("application") == "jmcomic-local-console"
                and health.get("pid") == pid and health.get("status") == "ok"):
            return port, record
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def code_fingerprint(root=ROOT):
    """Content hash of the RESTART_GLOBS files (relative path, size and bytes). app.py records it in
    flask.json at startup. Unlike timestamps it also catches an update copied over existing files (Windows
    keeps the old creation time, and the copied modification time can predate the server start) as well
    as added or removed files."""
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
        try:
            return fingerprint != code_fingerprint(root)
        except OSError:
            return True
    try:
        started = float(record.get("timestamp", 0))
    except (TypeError, ValueError):
        started = 0  # unknown start time: treat as outdated
    return code_updated_since(started, root)


def flush_server_logs(port, timeout=3):
    """Ask the server to write its pending log dedup summaries ("N similar messages merged"). Stopping it with
    taskkill /F skips its atexit flush, so this runs first. Best effort: any failure is ignored -- including a
    non-HTTP or malformed reply (http.client.BadStatusLine / LineTooLong are not OSErrors). Callers still run
    the stop in a finally block, so even a Ctrl+C during this wait cannot leave the server running."""
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
        except OSError:
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
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(handle)


def stop_outdated_server(pid, timeout=20, port=None, record=None):
    """Notify a verified service and wait for actual exit; never force an upgrade."""
    verified = get_running_server(expected_pid=pid)
    if verified is None or verified[0] != port:
        if not process_alive(pid):
            return
        raise RuntimeError("服务身份无法再次核实，已保留进程，请检查后重试")
    current = verified[1]
    if record is not None and (record.get("launch_token") != current.get("launch_token")
                               or record.get("shutdown_token") != current.get("shutdown_token")):
        raise RuntimeError("运行服务的身份已变化，已取消重启")
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
        venv_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        executable = str(venv_python) if venv_python.is_file() else sys.executable
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
            raise RuntimeError(f"Server exited during startup. See {get_log_dir() / LAUNCHER_LOG}.")
        # Windows venv python.exe may be a redirector with a different server PID.
        # The per-launch token follows its child while marker/health PIDs still match.
        port = get_running_port(expected_token=proc.jm_launch_token)
        if port is not None:
            return port
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Server startup timed out. See {get_log_dir() / LAUNCHER_LOG}.")


def main(argv=None, quiet=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", "-w", action="store_true", help="Press Enter to stop the server")
    args = parser.parse_args(argv)
    # Read-only preflight before opening or stopping a running instance. An old
    # launcher must never stop a newer service only to fail on its future schema.
    from core.database import _existing_schema
    _existing_schema()
    server = get_running_server()
    if server is not None:
        port, record = server
        if not server_outdated(record):
            webbrowser.open(f"http://{HOST}:{port}")
            return 0
        if not quiet:
            print(f"Program files changed since the running server (PID {record['pid']}) started; restarting it...")
        stop_outdated_server(int(record["pid"]), port=port, record=record)
    proc = launch_flask()
    keep_running = False
    try:
        port = wait_until_ready(proc)
        proc.jm_port = port  # cleanup() asks this server to flush its log summaries before stopping it
        url = f"http://{HOST}:{port}"
        webbrowser.open(url)
        if not quiet:
            print(f"Running: {url} (launcher child PID {proc.pid})")
        if args.wait:
            input("Press Enter or Ctrl+C to stop... ")
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
