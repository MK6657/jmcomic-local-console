"""Frozen launcher/entry dispatch and release boundaries, without spawning a service."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest


def test_portable_launcher_runs_owned_executable_without_source_python(tmp_path, monkeypatch):
    import launcher
    executable = tmp_path / "中文 便携目录" / "JMComic.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    popen = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    result = launcher.launch_flask(tmp_path)
    args, options = popen.call_args.args, popen.call_args.kwargs
    assert args == ([str(executable), "serve"],)
    assert options["cwd"] == str(executable.parent)
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert Path(options["env"]["JMCONSOLE_HOME"]) == tmp_path
    assert options["stdin"] == launcher.subprocess.DEVNULL
    assert options["stdout"] != launcher.subprocess.PIPE
    assert result.jm_launch_token == options["env"]["JMCONSOLE_LAUNCH_TOKEN"]


def test_frozen_fingerprint_detects_backend_and_dependency_updates(tmp_path, monkeypatch):
    import launcher
    executable = tmp_path / "JMComic.exe"
    metadata = tmp_path / "release.json"
    executable.write_bytes(b"frozen backend v1")
    metadata.write_text('{"packages":{"jmcomic":"test1"}}')
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    initial = launcher.code_fingerprint(tmp_path)
    record = {"code_fingerprint": initial}
    assert not launcher.server_outdated(record, root=tmp_path)
    executable.write_bytes(b"frozen backend v2")
    assert launcher.server_outdated(record, root=tmp_path)
    executable.write_bytes(b"frozen backend v1")
    metadata.write_text('{"packages":{"jmcomic":"test2"}}')
    assert launcher.server_outdated(record, root=tmp_path)


@pytest.mark.parametrize("command,module_name,target,expected", [
    ([], "launcher", "main", ["--wait"]),
    (["serve"], "app", "main", None),
    (["cli", "backup", "--output", "new.zip"], "jmcomic_cli", "main", ["backup", "--output", "new.zip"]),
])
def test_portable_entry_dispatches_existing_implementations(monkeypatch, command, module_name, target, expected):
    import desktop
    callback = Mock(return_value=0)
    monkeypatch.setitem(sys.modules, module_name, SimpleNamespace(**{target: callback}))
    assert desktop.main(command) == 0
    callback.assert_called_once_with(*([] if expected is None else [expected]))


def test_self_check_dispatch_reports_failure_and_propagates_exit_code(monkeypatch, capsys):
    import desktop
    result = {"status": "error", "exit_code": 1, "checks": {}, "error": "fixture failure"}
    monkeypatch.setitem(sys.modules, "core.release_check", SimpleNamespace(run_self_check=lambda: result))
    assert desktop.main(["self-check"]) == 1
    assert json.loads(capsys.readouterr().out) == result


def test_build_inputs_exclude_private_data_but_track_assets(tmp_path):
    from tools.build_windows import source_fingerprint
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "task.py").write_text("v1")
    initial = source_fingerprint(tmp_path)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "app.db").write_bytes(b"private")
    (tmp_path / "downloads").mkdir()
    (tmp_path / "downloads" / "page.webp").write_bytes(b"private")
    (tmp_path / ".env").write_text("private")
    assert source_fingerprint(tmp_path) == initial
    (tmp_path / "core" / "task.py").write_text("v2")
    assert source_fingerprint(tmp_path) != initial


def test_release_zip_contains_bundle_and_preserves_files(tmp_path):
    from tools.build_windows import zip_bundle
    bundle = tmp_path / "JMComic"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "JMComic.exe").write_bytes(b"exe")
    (bundle / "_internal" / "runtime.dll").write_bytes(b"dll")
    archive = tmp_path / "release.zip"
    zip_bundle(bundle, archive)
    with zipfile.ZipFile(archive) as output:
        assert output.testzip() is None
        assert sorted(output.namelist()) == ["JMComic/JMComic.exe", "JMComic/_internal/runtime.dll"]
        assert output.read("JMComic/_internal/runtime.dll") == b"dll"
    with pytest.raises(FileExistsError):
        zip_bundle(bundle, archive)


def test_optional_candidate_review_is_shipped_and_hashed_but_post_release_acceptance_is_neither(tmp_path):
    from tools.build_windows import copy_release_docs, source_fingerprint
    docs, bundle = tmp_path / 'docs', tmp_path / 'JMComic'
    docs.mkdir()
    bundle.mkdir()
    for name in ('WINDOWS_RELEASE.md', 'BACKEND_REVIEW.md', 'DATA_MAINTENANCE.md', 'AUDIT_FIXES.md'):
        (docs / name).write_text('required')
    initial = source_fingerprint(tmp_path)
    acceptance = 'WINDOWS_VNEXT_ACCEPTANCE.md'
    (docs / acceptance).write_text('results after release')
    copy_release_docs(tmp_path, bundle)
    assert not (bundle / acceptance).exists() and source_fingerprint(tmp_path) == initial
    review = 'WINDOWS_VNEXT_REVIEW.md'
    for content in (b'first candidate', b'updated handoff'):
        (docs / review).write_bytes(content)
        copy_release_docs(tmp_path, bundle)
        assert (bundle / review).read_bytes() == content and not (bundle / acceptance).exists()
        current = source_fingerprint(tmp_path)
        assert current != initial
        initial = current


@pytest.mark.parametrize("returncode,stdout", [
    (0, '{"status":"error","exit_code":1}'),
    (1, '{"status":"ok","exit_code":0}'),
    (0, "not JSON"),
])
def test_build_refuses_failed_or_unconfirmed_executable_check(tmp_path, monkeypatch, returncode, stdout):
    from tools import build_windows
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(build_windows.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=returncode, stdout=stdout)))
    with pytest.raises(RuntimeError):
        build_windows.check_bundle(tmp_path / "JMComic.exe", cwd=tmp_path)


def test_build_check_removes_developer_python_and_instance_paths(tmp_path, monkeypatch):
    from tools import build_windows
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", "developer-runtime")
    monkeypatch.setenv("JMCONSOLE_HOME", "private-instance")
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"status":"ok","exit_code":0}'))
    monkeypatch.setattr(build_windows.subprocess, "run", runner)
    assert build_windows.check_bundle(tmp_path / "JMComic.exe", cwd=tmp_path)["status"] == "ok"
    environment = runner.call_args.kwargs["env"]
    assert "PYTHONPATH" not in environment and "JMCONSOLE_HOME" not in environment
    assert environment["PATH"] == str(tmp_path / "System32")


def test_release_preflight_refuses_existing_target(tmp_path, monkeypatch):
    from tools import build_windows
    monkeypatch.setattr(build_windows, "ROOT", tmp_path)
    with pytest.raises(FileExistsError):
        build_windows.release_target(tmp_path, "fixture")
    assert build_windows.release_target(None, "fixture").parent == tmp_path / "releases"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows volume preflight")
def test_release_preflight_refuses_cross_volume_before_building(tmp_path, monkeypatch):
    from tools import build_windows
    monkeypatch.setattr(build_windows, "ROOT", tmp_path)
    other_drive = "D:" if tmp_path.drive.casefold() != "d:" else "C:"
    with pytest.raises(ValueError, match="同一盘"):
        build_windows.release_target(other_drive + r"\jmcomic-release-preflight-fixture\new", "fixture")
