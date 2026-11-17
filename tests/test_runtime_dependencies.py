from importlib import metadata
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from core.runtime_dependencies import installed_versions


def _install(site, name, version, requires=()):
    info = site / f"{name.replace('-', '_')}-1.dist-info"
    info.mkdir(exist_ok=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                                   + "".join(f"Requires-Dist: {item}\n" for item in requires), encoding="utf-8")


def test_only_runtime_closure_and_its_manifest_change_the_source_fingerprint(tmp_path, monkeypatch):
    import launcher
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.syspath_prepend(str(site))
    _install(site, "fixture-sdk", "1", ["Fixture_HTTP[images]", "fixture-dev; extra == 'tests'",
                                        "fixture-foreign; python_version < '2'"])
    _install(site, "fixture-http", "3", ["fixture-image; extra == 'images'", "fixture-sdk"])
    _install(site, "fixture-image", "7")
    runtime = tmp_path / "requirements.txt"
    runtime.write_text("Fixture_SDK>=1  # runtime\n")
    expected = [("fixture-http", "3"), ("fixture-image", "7"), ("fixture-sdk", "1")]
    assert installed_versions(runtime.read_text()) == expected
    record = {"code_fingerprint": launcher.code_fingerprint(tmp_path)}
    for kind in ("dev", "build"):
        (tmp_path / f"requirements-{kind}.txt").write_text(f"fixture-{kind}\n")
        _install(site, f"fixture-{kind}", "2")
    assert not launcher.server_outdated(record, tmp_path)
    _install(site, "fixture-image", "8")
    assert launcher.server_outdated(record, tmp_path)
    _install(site, "fixture-image", "7")
    assert not launcher.server_outdated(record, tmp_path)
    runtime.write_text("Fixture_SDK==1\n")
    os.utime(runtime, (100, 100))
    assert launcher.server_outdated(record, tmp_path)
    assert launcher.code_updated_since(99, tmp_path)
    with pytest.raises(metadata.PackageNotFoundError):
        installed_versions("fixture-sdk[tests]\nfixture-not-installed")


def test_compare_against_python_used_to_launch_the_service(tmp_path, monkeypatch):
    import launcher
    executable = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.touch()
    saved = "9" * 64
    probe = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=saved.encode() + b"\n"))
    monkeypatch.setattr(launcher.subprocess, "run", probe)
    monkeypatch.setattr(launcher, "code_fingerprint", Mock(side_effect=AssertionError("wrong interpreter")))
    assert Path(sys.executable) != executable
    assert not launcher.server_outdated({"code_fingerprint": saved}, tmp_path)
    assert probe.call_args.args[0][0] == str(executable)
    assert probe.call_args.kwargs["cwd"] == tmp_path
    assert probe.call_args.kwargs["timeout"] > 0
    probe.return_value.stdout = b"invalid response"
    with pytest.raises(ValueError, match="运行依赖"):
        launcher.server_outdated({"code_fingerprint": saved}, tmp_path)
