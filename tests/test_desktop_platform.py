"""Optional desktop capabilities never create directories on unsupported platforms."""
import os
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("capability", [None, "missing", False, "not callable"])
def test_unsupported_adapter_reports_capability_without_side_effects(tmp_path, monkeypatch, capability):
    from core import desktop_platform
    if capability == "missing":
        monkeypatch.delattr(os, "startfile", raising=False)
    else:
        monkeypatch.setattr(os, "startfile", capability, raising=False)
    target = tmp_path / "missing"
    assert not desktop_platform.supports_open_folder()
    with pytest.raises(desktop_platform.UnsupportedDesktopOperation, match="不支持"):
        desktop_platform.open_folder(target)
    assert not target.exists()


def test_adapter_passes_one_native_path_and_preserves_os_errors(tmp_path, monkeypatch):
    from core import desktop_platform
    opener = Mock()
    monkeypatch.setattr(os, "startfile", opener, raising=False)
    target = tmp_path / "folder"
    assert desktop_platform.supports_open_folder()
    desktop_platform.open_folder(target)
    opener.assert_called_once_with(str(target))
    assert not target.exists()  # adapter never creates or authorizes directories
    opener.side_effect = PermissionError("native denied")
    with pytest.raises(PermissionError, match="native denied"):
        desktop_platform.open_folder(target)


@pytest.fixture
def directories(client, tmp_path, monkeypatch):
    from core import path_guard
    from routes import api_system
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", downloads)
    logs = tmp_path / "missing-logs"
    monkeypatch.setattr(api_system, "_log_dir", lambda: logs)
    return downloads, logs


def _job(folder, status):
    from core import database as db
    db.insert_job("job_desktop", "123", "Desktop fixture", [])
    db.transition_job_status(
        "job_desktop", ["queued", "running", "paused", "completed", "failed", "canceled"], status,
        output_path=str(folder),
    )


@pytest.mark.parametrize("status", ["queued", "running", "paused", "completed", "failed", "canceled"])
@pytest.mark.parametrize("capability", [None, "missing"])
def test_download_route_without_opener_is_501_and_never_creates_folder(client, directories, monkeypatch, status, capability):
    from core import desktop_platform
    downloads, logs = directories
    target = downloads / "not-yet-created"
    _job(target, status)
    if capability == "missing":
        monkeypatch.delattr(os, "startfile", raising=False)
    else:
        monkeypatch.setattr(os, "startfile", None, raising=False)
    native = Mock(side_effect=AssertionError("unsupported operation must not delegate"))
    monkeypatch.setattr(desktop_platform, "open_folder", native)
    response = client.post("/api/jobs/job_desktop/open-folder")
    assert response.status_code == 501
    assert "不支持" in response.get_json()["message"]
    assert not target.exists()
    native.assert_not_called()


@pytest.mark.parametrize("capability", [None, "missing"])
def test_log_route_without_opener_is_501_and_has_no_creation_side_effect(client, directories, monkeypatch, capability):
    from core import desktop_platform
    _, logs = directories
    if capability == "missing":
        monkeypatch.delattr(os, "startfile", raising=False)
    else:
        monkeypatch.setattr(os, "startfile", None, raising=False)
    native = Mock()
    monkeypatch.setattr(desktop_platform, "open_folder", native)
    response = client.post("/api/system/logs/open-folder")
    assert response.status_code == 501
    assert not logs.exists()
    native.assert_not_called()


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
def test_desktop_route_preserves_missing_finished_directory_rule(client, directories, monkeypatch, status):
    downloads, _ = directories
    target = downloads / "deleted"
    _job(target, status)
    opener = Mock()
    monkeypatch.setattr(os, "startfile", opener, raising=False)
    response = client.post("/api/jobs/job_desktop/open-folder")
    assert response.status_code == 404
    assert not target.exists()
    opener.assert_not_called()


def test_desktop_routes_use_native_one_argument_and_keep_path_checks(client, directories, tmp_path, monkeypatch):
    from core import database as db
    downloads, logs = directories
    target = downloads / "active"
    _job(target, "queued")
    opener = Mock()
    monkeypatch.setattr(os, "startfile", opener, raising=False)
    assert client.post("/api/jobs/job_desktop/open-folder").status_code == 200
    assert target.is_dir()
    opener.assert_called_once_with(str(target))
    opener.reset_mock()
    assert client.post("/api/system/logs/open-folder").status_code == 200
    assert logs.is_dir()
    opener.assert_called_once_with(str(logs))
    opener.reset_mock()
    outside = tmp_path / "outside"
    db.update_job("job_desktop", output_path=str(outside))
    assert client.post("/api/jobs/job_desktop/open-folder").status_code == 403
    assert not outside.exists()
    opener.assert_not_called()


def test_download_route_native_failure_is_reported_without_shell_fallback(client, directories, monkeypatch):
    downloads, _ = directories
    target = downloads / "existing"
    target.mkdir()
    _job(target, "completed")
    opener = Mock(side_effect=PermissionError("denied"))
    monkeypatch.setattr(os, "startfile", opener, raising=False)
    response = client.post("/api/jobs/job_desktop/open-folder")
    assert response.status_code == 500
    opener.assert_called_once_with(str(target))


def test_no_desktop_capability_does_not_bypass_authorization(client, directories, tmp_path, monkeypatch):
    downloads, _ = directories
    outside = tmp_path / "outside"
    _job(outside, "queued")
    monkeypatch.delattr(os, "startfile", raising=False)
    assert client.post("/api/jobs/job_desktop/open-folder").status_code == 403
    assert not outside.exists()
    assert client.post("/api/jobs/job_missing/open-folder").status_code == 404


def test_explorer_foreground_matcher_handles_new_window_tab_switch_and_existing(tmp_path, monkeypatch):
    from core import desktop_platform

    folder = tmp_path / "导出"
    assert desktop_platform._title_matches_folder("导出 - 文件资源管理器", folder)
    assert desktop_platform._title_matches_folder(f"{folder} - File Explorer", folder)
    assert not desktop_platform._title_matches_folder("下载 - 文件资源管理器", folder)

    focused = []
    monkeypatch.setattr(desktop_platform, "_force_window_foreground", focused.append)
    monkeypatch.setattr(desktop_platform.time, "sleep", lambda _s: None)

    # Tier 1: brand-new Explorer window opened
    monkeypatch.setattr(desktop_platform, "_snapshot_explorer_windows", lambda: {101: "下载 - 文件资源管理器", 202: "导出 - 文件资源管理器"})
    desktop_platform._focus_explorer_window(folder, {101: "下载 - 文件资源管理器"})
    assert focused == [202]

    # Tier 2: Windows 11 tab added inside an existing Explorer window (title changed)
    focused.clear()
    monkeypatch.setattr(desktop_platform, "_snapshot_explorer_windows", lambda: {101: "导出 - 文件资源管理器"})
    desktop_platform._focus_explorer_window(folder, {101: "下载 - 文件资源管理器"})
    assert focused == [101]


def test_export_open_folder_route_respects_capability_and_file_selection(client, tmp_path, monkeypatch):
    from core import desktop_platform
    from routes import api_export

    export_dir = tmp_path / "导出"
    monkeypatch.setattr(api_export, "get_export_dir", lambda: export_dir)

    # Unsupported platform -> 501 without creating export directory
    monkeypatch.delattr(os, "startfile", raising=False)
    res = client.post("/api/export/open-folder")
    assert res.status_code == 501
    assert not export_dir.exists()

    # Supported platform -> creates directory and opens/selects safely
    calls = []
    monkeypatch.setattr(os, "startfile", Mock(), raising=False)
    monkeypatch.setattr(desktop_platform, "open_folder", lambda path, *, select=None: calls.append((path, select)))

    res = client.post("/api/export/open-folder")
    assert res.status_code == 200 and export_dir.is_dir()
    assert calls[-1] == (export_dir, None)

    sample = export_dir / "Sample_123.cbz"
    sample.write_bytes(b"PK")
    res = client.post("/api/export/open-folder", json={"filename": "Sample_123.cbz"})
    assert res.status_code == 200
    assert calls[-1] == (export_dir, sample)

    # Traversal filename is ignored (opens folder only, never selects outside file)
    res = client.post("/api/export/open-folder", json={"filename": "../secret.txt"})
    assert res.status_code == 200
    assert calls[-1] == (export_dir, None)
