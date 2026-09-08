"""Start the local console without killing other programs or blocking log pipes."""
import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

HOST = "127.0.0.1"
ROOT = Path(__file__).resolve().parent
FLASK_JSON = ROOT / "runtime" / "data" / "flask.json"
MAX_WAIT = 30
POLL_INTERVAL = 0.5


def get_running_port(expected_pid=None, expected_token=None):
    """Require both our startup record and a matching application/PID response."""
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
            return port
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def launch_flask(root=ROOT):
    """Use the project interpreter and a file, never undrained PIPEs."""
    venv_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = str(venv_python) if venv_python.is_file() else sys.executable
    log_dir = root / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    token = uuid.uuid4().hex
    environment = dict(os.environ, JMCONSOLE_LAUNCH_TOKEN=token)
    with (log_dir / "launcher.log").open("ab") as output:
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
    port = get_running_port()
    if port is not None:
        webbrowser.open(f"http://{HOST}:{port}")
        return 0
    proc = launch_flask()
    keep_running = False
    try:
        port = wait_until_ready(proc)
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
