"""A new SDK dependency cannot silently enter the portable runtime unpinned."""
import pytest
from types import SimpleNamespace

from tools import build_windows


def test_release_refuses_unpinned_or_drifted_transitive_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(build_windows, "ROOT", tmp_path)
    (tmp_path / "requirements-build.txt").write_text("-r requirements.txt\n")
    (tmp_path / "requirements.txt").write_text("jmcomic==2.7.10\n")
    pins = tmp_path / "requirements-windows-build-constraints.txt"
    pins.write_text("jmcomic==2.7.10\n")
    installed = {"jmcomic": "2.7.10", "curl-cffi": "0.17.0"}
    monkeypatch.setattr(build_windows.importlib.metadata, "distribution", lambda name: SimpleNamespace(
        version=installed[name], requires=["curl_cffi>=0.17.0", 'test-only; extra == "testing"'] if name == "jmcomic" else []))
    with pytest.raises(RuntimeError, match="curl-cffi.*缺少"):
        build_windows.verified_packages()
    pins.write_text("jmcomic==2.7.10\ncurl-cffi==0.17.0\n")
    assert build_windows.verified_packages() == installed
    installed["curl-cffi"] = "0.18.0"
    with pytest.raises(RuntimeError, match="curl-cffi.*0.18.0"):
        build_windows.verified_packages()
