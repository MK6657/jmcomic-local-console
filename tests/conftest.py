"""Offline regression fixtures. Never use the user's runtime directory."""
import atexit
import logging
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Legacy scripts perform network requests and mutations at import time.
# Keep them for reference, but never import them during pytest collection.
collect_ignore = [
    "test_smoke.py", "test_endpoints.py", "test_key_e2e.py",
    "test_e2e_integration.py", "test_export_integrity.py", "test_stress.py",
]

# The app root is redirected here, before any test module is imported. core binds module-level paths when it is
# first imported (core.database DB_DIR / DB_PATH, core.path_guard DOWNLOAD_ROOT, core.logger LOG_DIR and its open
# log files, core.online_reader CACHE_ROOT, app._PID_FILE / _PORT_FILE), so whatever imports core first — a test
# module at collection, a helper, a fixture — binds them to this temporary root, never to the real runtime/ or
# downloads/. The session `app` fixture checks that before create_app() runs db.init_db().
import core.path_utils as _paths  # noqa: E402  (only sys / pathlib: importing it binds nothing)

if any(name == "app" or name.startswith(("core.", "routes.")) for name in sys.modules if name != "core.path_utils"):
    raise RuntimeError("core was imported before tests/conftest.py redirected the app root")
TEST_APP_ROOT = Path(tempfile.mkdtemp(prefix="jmcomic-tests-")).resolve()
_paths.get_app_root = lambda: TEST_APP_ROOT
REAL_RUNTIME_DIRS = (REPO_ROOT / "runtime", REPO_ROOT / "downloads")


def _remove_test_app_root():
    """At exit, after core.logger's own atexit shutdown (registered later, so it runs first) has written its last
    summaries: detach and close its log files (Windows cannot delete open files), then delete the temporary root."""
    logger = sys.modules.get("core.logger")
    handlers = list(getattr(logger, "_shared_handlers", None) or []) if logger is not None else []
    loggers = [logging.getLogger()] + [each for each in logging.Logger.manager.loggerDict.values()
                                       if isinstance(each, logging.Logger)]
    for handler in handlers:
        for each in loggers:
            each.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    shutil.rmtree(TEST_APP_ROOT, ignore_errors=True)


atexit.register(_remove_test_app_root)


def _isolated_path(path, *roots) -> bool:
    real = Path(path).resolve()
    return (any(real.is_relative_to(Path(root).resolve()) for root in roots)
            and not any(real.is_relative_to(d.resolve()) for d in REAL_RUNTIME_DIRS))


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    from core import database as db, path_guard
    # create_app() runs db.init_db() at once: prove it cannot reach the real runtime/ or downloads/
    assert TEST_APP_ROOT.is_relative_to(Path(tempfile.gettempdir()).resolve())
    for path in (db.DB_DIR, db.DB_PATH, path_guard.DOWNLOAD_ROOT):
        assert _isolated_path(path, TEST_APP_ROOT, tmp_path_factory.getbasetemp()), \
            f"test isolation broken: {path} is not inside a temporary directory"
    from app import create_app
    application = create_app()
    application.config["TESTING"] = True
    yield application


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network access is forbidden in regression tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    # jmcomic's HTTP goes through curl_cffi (libcurl), which never touches Python sockets
    from curl_cffi.curl import Curl
    monkeypatch.setattr(Curl, "perform", blocked)


@pytest.fixture
def client(app, tmp_path, monkeypatch):
    from core import database as db
    from core.settings import invalidate_settings_cache
    from core.jm_service import invalidate_option_cache
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()
    invalidate_settings_cache()
    invalidate_option_cache()
    yield app.test_client()
    invalidate_settings_cache()
    invalidate_option_cache()
