"""launcher.log housekeeping: rotation before a launch and 7-day retention (temp dirs only)."""
import os
import time
from unittest.mock import Mock


def _day(timestamp):
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def _age(path, days):
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return stamp


def test_large_launcher_log_is_rotated(tmp_path, monkeypatch):
    import launcher
    monkeypatch.setattr(launcher, "LAUNCHER_LOG_MAX_BYTES", 100)
    active = tmp_path / "launcher.log"
    active.write_bytes(b"x" * 200)
    (tmp_path / f"launcher.log.{_day(time.time())}").write_bytes(b"earlier today")
    launcher.rotate_launcher_log(tmp_path)
    assert not active.exists()
    rotated = tmp_path / f"launcher.log.{_day(time.time())}.1"
    assert rotated.read_bytes() == b"x" * 200
    assert (tmp_path / f"launcher.log.{_day(time.time())}").read_bytes() == b"earlier today"


def test_launcher_log_from_an_earlier_day_is_rotated(tmp_path):
    import launcher
    active = tmp_path / "launcher.log"
    active.write_bytes(b"yesterday\n")
    stamp = _age(active, 1)
    launcher.rotate_launcher_log(tmp_path)
    assert not active.exists()
    assert (tmp_path / f"launcher.log.{_day(stamp)}").read_bytes() == b"yesterday\n"


def test_small_current_launcher_log_is_kept(tmp_path):
    import launcher
    active = tmp_path / "launcher.log"
    active.write_bytes(b"today\n")
    launcher.rotate_launcher_log(tmp_path)
    assert active.read_bytes() == b"today\n"
    assert [p.name for p in tmp_path.iterdir()] == ["launcher.log"]


def test_old_rotated_launcher_logs_are_deleted(tmp_path):
    import launcher
    for name, days in (("launcher.log.2026-09-01", 8), ("launcher.log.2026-09-01.1", 9),
                       ("launcher.log.2026-09-20", 3), ("app.log.2026-09-01", 30), ("notes.txt", 30)):
        (tmp_path / name).write_bytes(b"x")
        _age(tmp_path / name, days)
    launcher.rotate_launcher_log(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "app.log.2026-09-01", "launcher.log.2026-09-20", "notes.txt"]


def test_locked_launcher_log_is_tolerated(tmp_path, monkeypatch):
    import launcher
    active = tmp_path / "launcher.log"
    active.write_bytes(b"held open by a running server\n")
    _age(active, 1)

    def locked(*args, **kwargs):
        raise PermissionError("in use")
    monkeypatch.setattr(os, "rename", locked)
    launcher.rotate_launcher_log(tmp_path)
    assert active.exists()


def test_launch_rotates_then_appends_utf8_marker(tmp_path, monkeypatch):
    import launcher
    popen = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    log_dir = tmp_path / "runtime" / "logs"
    log_dir.mkdir(parents=True)
    active = log_dir / "launcher.log"
    active.write_bytes(b"old run\n")
    stamp = _age(active, 1)
    launcher.launch_flask(tmp_path)
    assert (log_dir / f"launcher.log.{_day(stamp)}").read_bytes() == b"old run\n"
    content = active.read_text(encoding="utf-8")
    assert "[INFO] [launcher] starting server:" in content and "old run" not in content
    assert popen.call_args.kwargs["env"]["PYTHONIOENCODING"].startswith("utf-8")
