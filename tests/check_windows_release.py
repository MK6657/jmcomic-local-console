"""Opt-in ZIP/executable smoke check; temporary fixtures and loopback only.

Run with the new release directory, optionally --check-launcher to verify that
the frozen launcher refuses a busy production mutex without opening a browser.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PATH_KEYS = ("JMCONSOLE_HOME", "JMCONSOLE_DOWNLOAD_DIR", "JMCONSOLE_CACHE_DIR", "JMCONSOLE_BACKUP_DIR")


def environment(home):
    result = {key: value for key, value in os.environ.items()
              if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} and not key.startswith("JMCONSOLE_")}
    result["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    result.update(dict(zip(PATH_KEYS, map(str, (home, home / "downloads", home / "cache", home / "backups")))))
    return result


def check(release, check_launcher=False):
    archive = release / "JMComic-windows-x64.zip"
    expected, filename = (release / "SHA256SUMS.txt").read_text(encoding="ascii").strip().split()
    assert filename == archive.name
    with archive.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == expected
    with tempfile.TemporaryDirectory(prefix="jmcomic-portable-zip-") as temporary:
        root = Path(temporary)
        extracted = root / "中文 解压验证"
        unrelated = root / "unrelated-working-directory"
        unrelated.mkdir()
        with zipfile.ZipFile(archive) as package:
            assert package.testzip() is None
            for member in package.infolist():
                assert member.filename.startswith("JMComic/")
                assert not member.filename.startswith(("JMComic/runtime/", "JMComic/downloads/", "JMComic/.venv/"))
                path = extracted / member.filename
                assert path.resolve().is_relative_to(extracted.resolve())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(package.read(member))
        executable = extracted / "JMComic" / "JMComic.exe"
        untouched = root / "inherited-instance-must-not-be-opened"
        process = subprocess.run([str(executable), "self-check"], cwd=unrelated, env=environment(untouched),
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
        assert process.returncode == 0, "extracted executable self-check failed"
        report = json.loads(process.stdout.strip().splitlines()[-1])
        assert report["status"] == "ok" and report["exit_code"] == 0 and report["checks"]["cleanup"]
        assert not untouched.exists() and not (executable.parent / "runtime").exists()
        # Build a minimal empty SQLite fixture; no user records or source app
        # startup is needed to exercise the frozen backup validation command.
        fixture = root / "fixture.db"
        with closing(sqlite3.connect(fixture)) as connection:
            with connection:
                connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
                connection.execute("CREATE TABLE jobs (job_id TEXT, album_id TEXT, status TEXT, output_path TEXT)")
        cli_home = root / "cli-instance"
        os.environ.update({key: value for key, value in environment(cli_home).items() if key in PATH_KEYS})
        from core.backup import create_backup
        downloads = root / "fixture-downloads"
        downloads.mkdir()
        backup = root / "fixture.zip"
        create_backup(fixture, downloads, backup)
        process = subprocess.run([str(executable), "cli", "backup-info", str(backup)], cwd=unrelated,
                                 env=environment(cli_home), stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=15)
        assert process.returncode == 0, "portable backup-info failed"
        launcher_checked = False
        if check_launcher:
            from core.instance_lock import acquire_instance_lock, InstanceBusyError
            lease = None
            try:
                try:
                    lease = acquire_instance_lock()
                except InstanceBusyError:
                    pass  # The daily instance may already own it; never stop it.
                launch_home = root / "launcher-instance"
                process = subprocess.run([str(executable)], cwd=unrelated, env=environment(launch_home),
                                         stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                         encoding="utf-8", errors="replace", timeout=15)
                assert process.returncode == 1, "busy mutex must stop the owned child"
                log = (launch_home / "runtime/logs/launcher.log").read_text(encoding="utf-8")
                assert "Another JMComic instance" in log
                assert not (launch_home / "runtime/data/flask.json").exists()
                launcher_checked = True
            finally:
                if lease is not None:
                    lease.close()
    return {"zip_sha256_verified": True, "extracted_self_check": True, "unicode_path": True,
            "system_python_removed_from_path": True, "inherited_instance_untouched": True,
            "portable_backup_info": True, "busy_launcher_rejected": launcher_checked, "upstream_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("--check-launcher", action="store_true")
    options = parser.parse_args()
    print(json.dumps(check(options.release.resolve(), options.check_launcher)))
