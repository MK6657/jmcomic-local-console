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

HOST = "127.0.0.1"
ROOT = Path(__file__).resolve().parent
FLASK_JSON = ROOT / "runtime" / "data" / "flask.json"
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


def stop_outdated_server(pid, timeout=10, port=None):
    """Stop the server whose identity get_running_server() verified. Interrupted downloads are
    re-queued by app.py at the next start."""
    try:
        flush_server_logs(port)
    finally:  # the stop must happen even if the flush raises or is interrupted (Ctrl+C)
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=10,
            )
        else:
            os.kill(pid, 15)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"Could not stop the outdated server (PID {pid}); close it and run start.bat again.")


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


def launch_flask(root=ROOT):
    """Use the project interpreter and a file, never undrained PIPEs."""
    venv_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = str(venv_python) if venv_python.is_file() else sys.executable
    log_dir = root / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        rotate_launcher_log(log_dir)
    except Exception:
        pass  # housekeeping must never block a launch
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    token = uuid.uuid4().hex
    environment = dict(os.environ, JMCONSOLE_LAUNCH_TOKEN=token)
    # Child output lands in launcher.log: keep it UTF-8 (not the console code page) so it stays readable.
    environment["PYTHONIOENCODING"] = "utf-8:backslashreplace"
    with (log_dir / LAUNCHER_LOG).open("ab") as output:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        output.write(f"[{stamp}] [INFO] [launcher] starting server: {executable} app.py\n".encode("utf-8"))
        output.flush()
        proc = subprocess.Popen(
            [executable, str(root / "app.py")], cwd=str(root),
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            env=environment, **kwargs,
        )
    proc.jm_launch_token = token
    return proc


def cleanup(proc):
    """Only stop our live child tree, including Windows venv redirector children."""
    if proc.poll() is None:
        try:
            flush_server_logs(getattr(proc, "jm_port", None))
        finally:  # the stop must happen even if the flush raises or is interrupted (Ctrl+C)
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=10,
                )
                if result.returncode and proc.poll() is None:
                    raise RuntimeError("Could not stop the owned server process tree")
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def wait_until_ready(proc, timeout=MAX_WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Server exited during startup. See runtime/logs/launcher.log.")
        # Windows venv python.exe may be a redirector with a different server PID.
        # The per-launch token follows its child while marker/health PIDs still match.
        port = get_running_port(expected_token=proc.jm_launch_token)
        if port is not None:
            return port
        time.sleep(POLL_INTERVAL)
    raise TimeoutError("Server startup timed out. See runtime/logs/launcher.log.")


def main(argv=None, quiet=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", "-w", action="store_true", help="Press Enter to stop the server")
    args = parser.parse_args(argv)
    server = get_running_server()
    if server is not None:
        port, record = server
        if not server_outdated(record):
            webbrowser.open(f"http://{HOST}:{port}")
            return 0
        if not quiet:
            print(f"Program files changed since the running server (PID {record['pid']}) started; restarting it...")
        stop_outdated_server(int(record["pid"]), port=port)
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
