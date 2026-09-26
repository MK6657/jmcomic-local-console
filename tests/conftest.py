"""Offline regression fixtures. Never use the user's runtime directory."""
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Legacy scripts perform network requests and mutations at import time.
# Keep them for reference, but never import them during pytest collection.
collect_ignore = [
    "test_smoke.py", "test_endpoints.py", "test_key_e2e.py",
    "test_e2e_integration.py", "test_export_integrity.py", "test_stress.py",
]


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    import core.path_utils as paths
    with pytest.MonkeyPatch.context() as patch:
        root = tmp_path_factory.mktemp("jmcomic-runtime")
        patch.setattr(paths, "get_app_root", lambda: root)
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
