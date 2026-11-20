"""Frozen launcher/entry dispatch and release boundaries, without spawning a service."""
import builtins
from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tracemalloc
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


def test_frozen_fingerprint_preserves_digest_with_bounded_allocation(tmp_path, monkeypatch):
    import launcher

    executable, metadata = tmp_path / "JMComic.exe", tmp_path / "release.json"
    block, manifest = b"fixture!" * 131072, b'{"format":2}'
    with executable.open("wb") as handle:
        for _ in range(16):
            handle.write(block)
    metadata.write_bytes(manifest)
    expected = hashlib.sha256(block * 16 + manifest).hexdigest()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    tracemalloc.start()
    try:
        assert launcher.code_fingerprint(tmp_path) == expected
        assert tracemalloc.get_traced_memory()[1] < 2 * len(block)
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("command,module_name,target,expected", [
    ([], "launcher", "main", ["--wait"]),
    (["cli", "backup", "--output", "new.zip"], "jmcomic_cli", "main", ["backup", "--output", "new.zip"]),
])
def test_portable_entry_dispatches_existing_implementations(monkeypatch, command, module_name, target, expected):
    import desktop
    callback = Mock(return_value=0)
    monkeypatch.setitem(sys.modules, module_name, SimpleNamespace(**{target: callback}))
    assert desktop.main(command) == 0
    callback.assert_called_once_with(expected)


@pytest.mark.parametrize("scenario", ["busy", "import_failed", "serve"])
def test_portable_serve_owns_instance_before_import(scenario, monkeypatch):
    import desktop
    from core import instance_lock

    lease = Mock()
    claim = Mock(return_value=lease)
    if scenario == "busy":
        claim.side_effect = instance_lock.InstanceBusyError("fixture instance busy")
    monkeypatch.setattr(instance_lock, "acquire_instance_lock", claim)
    serve = Mock(return_value=0)
    import_app = Mock(return_value=SimpleNamespace(main=serve))
    if scenario == "import_failed":
        import_app.side_effect = ImportError("fixture dependency missing")
    original_import = builtins.__import__

    def check_import(name, *args, **kwargs):
        if name == "app":
            claim.assert_called_once_with()
            return import_app()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", check_import)
    if scenario == "busy":
        with pytest.raises(instance_lock.InstanceBusyError):
            desktop.main(["serve"])
        import_app.assert_not_called()
        serve.assert_not_called()
    elif scenario == "import_failed":
        with pytest.raises(ImportError):
            desktop.main(["serve"])
        lease.close.assert_called_once_with()
    else:
        assert desktop.main(["serve"]) == 0
        serve.assert_called_once_with(instance_lease=lease)
        lease.close.assert_not_called()


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


def test_release_docs_also_resolve_from_docs_archive(tmp_path):
    from tools.build_windows import copy_release_docs, source_fingerprint
    docs, archive, bundle = tmp_path / 'docs', tmp_path / 'docs' / 'archive', tmp_path / 'JMComic'
    archive.mkdir(parents=True)
    bundle.mkdir()
    (docs / 'WINDOWS_RELEASE.md').write_text('[review](archive/BACKEND_REVIEW.md)', encoding="utf-8")
    (docs / 'DATA_MAINTENANCE.md').write_text('active-data')
    (archive / 'BACKEND_REVIEW.md').write_text('archived-backend')
    (archive / 'AUDIT_FIXES.md').write_text('archived-audit')
    (archive / 'WINDOWS_VNEXT_REVIEW.md').write_text('archived-vnext-review')
    (archive / 'WINDOWS_VNEXT_ACCEPTANCE.md').write_text('post-release-acceptance')
    fp1 = source_fingerprint(tmp_path)
    copy_release_docs(tmp_path, bundle)
    assert (bundle / 'README-Windows.md').read_text(encoding="utf-8") == '[review](BACKEND_REVIEW.md)'
    assert (docs / 'WINDOWS_RELEASE.md').read_text(encoding="utf-8") == '[review](archive/BACKEND_REVIEW.md)'
    assert (bundle / 'BACKEND_REVIEW.md').read_text() == 'archived-backend'
    assert (bundle / 'AUDIT_FIXES.md').read_text() == 'archived-audit'
    assert (bundle / 'WINDOWS_VNEXT_REVIEW.md').read_text() == 'archived-vnext-review'
    assert not (bundle / 'WINDOWS_VNEXT_ACCEPTANCE.md').exists()
    (archive / 'WINDOWS_VNEXT_REVIEW.md').write_text('archived-vnext-review-v2')
    assert source_fingerprint(tmp_path) != fp1


def test_portable_workflow_covers_release_inputs_and_acceptance_scripts():
    from tools.build_windows import ROOT, SOURCE_PATTERNS

    workflow = (ROOT / ".github/workflows/windows-portable.yml").read_text(encoding="utf-8")
    paths_block = workflow.split("\n    paths:\n", 1)[1].split("\n\n", 1)[0]
    filters = tuple(line.strip()[3:-1] for line in paths_block.splitlines() if line.strip().startswith("- '"))
    inputs = {path.relative_to(ROOT).as_posix() for pattern in SOURCE_PATTERNS
              for path in ROOT.glob(pattern) if path.is_file()}
    inputs.update({"launcher.pyw", "start.bat", "pytest.ini", "tests/check_startup.py",
                   "tests/checks/check_download_queue.py", "tests/checks/check_atelier_release.py",
                   "tests/checks/check_windows_lifecycle.py", "tests/checks/check_windows_release.py",
                   "tests/checks/check_vnext_portable.py"})
    assert {path for path in inputs if not any(fnmatchcase(path, entry) for entry in filters)} == set()


def test_source_zip_uses_allowlist_and_is_byte_reproducible(tmp_path, monkeypatch):
    from tools import package_atelier_source

    source = tmp_path / "checkout"
    included = {
        "app.py", "README.zh-CN.md", "requirements-dev.txt", "templates/partials/item.html",
        ".github/workflows/windows-portable.yml",
        "docs/WINDOWS_RELEASE.md", "docs/architecture/design.md", "docs/archive/BACKEND_REVIEW.md",
        "tests/test_app.py", "tests/checks/check_release.py", "tests/legacy/test_legacy.py",
        "tests/js/browser.cjs", "static/vendor/fonts/test.woff2", "tools/licenses/library/LICENSE",
    }
    excluded = {
        "local-audit.md", "requirements-local.txt", "docs/reports/synthetic.md",
        "tests/_test_export_data/synthetic.py", "tests/unused/synthetic.py",
        "static/private.db", "static/vendor/private.log", "static/vendor/.env",
        "runtime/private.json", "downloads/private-manifest.md",
    }
    for name in included | excluded:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode("utf-8"))
    monkeypatch.setattr(package_atelier_source, "ROOT", source)
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    assert package_atelier_source.write_source(first) == len(included)
    changed = source / "app.py"
    os.utime(changed, (1100000000, 1100000000))
    changed.chmod(0o444)
    try:
        assert package_atelier_source.write_source(second) == len(included)
    finally:
        changed.chmod(0o644)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {f"atelier-edition/{name}" for name in included}
        for entry in archive.infolist():
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
            assert entry.external_attr >> 16 == 0o100644
            assert archive.read(entry) == entry.filename.split("/", 1)[1].encode("utf-8")
        assert archive.testzip() is None


@pytest.mark.parametrize("process_code,report,expected", [
    (0, {"status": "ok", "exit_code": 0}, 0),
    (1, {"status": "ok", "exit_code": 0}, 1),
    (0, {"status": "ok", "exit_code": 1}, 1),
    (0, {"status": "ok", "exit_code": False}, 1),
    (0, {"status": "ok"}, 1),
])
def test_lifecycle_driver_validates_both_exit_codes(monkeypatch, process_code, report, expected):
    from tools.build_windows import ROOT
    import subprocess

    runner = Mock(return_value=SimpleNamespace(returncode=process_code, stdout=json.dumps(report)))
    monkeypatch.setattr(subprocess, "run", runner)
    driver = runpy.run_path(str(ROOT / "tests/checks/check_windows_lifecycle.py"))
    assert driver["main"]([]) == expected
    assert runner.call_args.args[0][-1] == "lifecycle-check"


def test_startup_driver_prints_the_verified_server_pid(tmp_path, monkeypatch, capsys):
    from tools.build_windows import ROOT
    import launcher
    import subprocess

    child = Mock(pid=4242)
    child.poll.return_value = 0
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=child))
    monkeypatch.setattr(launcher, "wait_until_ready", lambda process: 5001)
    getter = Mock(return_value=(5001, {"pid": 4040}))
    monkeypatch.setattr(launcher, "get_running_server", getter)
    driver = runpy.run_path(str(ROOT / "tests/check_startup.py"))
    driver["check_case"](False)
    getter.assert_called_once_with(expected_token=child.jm_launch_token)
    output = capsys.readouterr().out
    assert "PID 4040" in output and "4242" not in output


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
