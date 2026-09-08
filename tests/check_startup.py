"""Explicit local HTTP smoke check with disposable data; no upstream requests.

Run: python tests/check_startup.py
Not part of default pytest collection (it intentionally opens local sockets).
"""
import os
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import launcher

CHILD = """
import os, sys
from pathlib import Path
import core.path_utils as paths
paths.get_app_root = lambda: Path(sys.argv[1])
import app
app._MUTEX_NAME = 'Local\\\\JMComic_StartupCheck_' + str(os.getpid())
app.main()
"""


def check_case(occupied):
    holder = None
    if occupied:
        holder = socket.socket()
        try:
            holder.bind(("127.0.0.1", 5000))
            holder.listen()
        except OSError:
            holder.close()
            print("SKIP controlled port conflict: port 5000 already occupied")
            return
    try:
        with tempfile.TemporaryDirectory(prefix="jmcomic-startup-") as sandbox:
            root = Path(sandbox)
            marker = launcher.FLASK_JSON
            launcher.FLASK_JSON = root / "runtime/data/flask.json"
            try:
                with (root / "child.log").open("wb") as output:
                    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                    token = uuid.uuid4().hex
                    proc = subprocess.Popen(
                        [sys.executable, "-c", CHILD, sandbox], cwd=ROOT,
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                        env=dict(os.environ, JMCONSOLE_LAUNCH_TOKEN=token),
                        **kwargs,
                    )
                    proc.jm_launch_token = token
                    try:
                        port = launcher.wait_until_ready(proc)
                        assert launcher.get_running_port(expected_token=token) == port
                        if occupied:
                            assert port in (5001, 5002, 5003)
                            assert holder.getsockname()[1] == 5000
                        print(f"PASS {'fallback' if occupied else 'startup'}: verified port {port}, PID {proc.pid}")
                    finally:
                        launcher.cleanup(proc)
            finally:
                launcher.FLASK_JSON = marker
    finally:
        if holder is not None:
            holder.close()


if __name__ == "__main__":
    check_case(False)
    check_case(True)
