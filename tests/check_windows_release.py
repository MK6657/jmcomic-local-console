"""Opt-in ZIP/executable smoke check; temporary fixtures and loopback only.

Run with the new release directory, optionally --check-launcher to verify a
busy mutex, or --check-restore to exercise the ordinary offline maintenance CLI.
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


def seed_legacy_instance(home):
    """Synthetic v2 layout from before the resource catalog and queue migration."""
    from core.database_schema import BASE_SCHEMA_SQL
    from PIL import Image
    image = home / "downloads" / "Fixture_900001" / "01" / "00001.png"
    image.parent.mkdir(parents=True)
    with Image.new("RGB", (9, 7), (36, 84, 48)) as pixels:
        pixels.save(image)
    database = home / "runtime" / "data" / "app.db"
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(BASE_SCHEMA_SQL)
        connection.execute("PRAGMA user_version=2")
        connection.execute("INSERT INTO jobs(job_id,album_id,title,status,selected_photo_ids,output_path,done_pages,total_pages) "
                           "VALUES('fixture_book','900001','Fixture','completed','[]',?,1,1)", (str(image.parents[1]),))
        connection.execute("INSERT INTO jobs(job_id,album_id,title,status,selected_photo_ids) "
                           "VALUES('fixture_pause','900002','Fixture paused','paused','[]')")
        connection.executemany("INSERT INTO settings(key,value) VALUES(?,?)", [
            ('auto_update_check', 'false'), ('schedule_enabled', 'true'), ('schedule_start', '0'), ('schedule_end', '0')])
    return image


def portable_cli(executable, cwd, home, *args, download_root=None, expected_exit=0):
    env = environment(home)
    if download_root is not None:
        env['JMCONSOLE_DOWNLOAD_DIR'] = str(download_root)
    run = subprocess.run([str(executable), "cli", *map(str, args)], cwd=cwd, env=env,
                         stdin=subprocess.DEVNULL, capture_output=True, timeout=45)
    assert run.returncode == expected_exit, f"portable {args[0]} returned {run.returncode}, expected {expected_exit}"
    return run


def check(release, check_launcher=False, check_restore=False):
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
        cli_home = root / "cli-instance"
        os.environ.update({key: value for key, value in environment(cli_home).items() if key in PATH_KEYS})
        image = seed_legacy_instance(cli_home)
        backup = root / "fixture.zip"
        if check_restore:
            portable_cli(executable, unrelated, cli_home, "backup", "--include-downloads", "--output", backup)
        else:
            # This path also works while the daily instance holds the service lock.
            from core.backup import create_backup
            create_backup(cli_home / "runtime/data/app.db", cli_home / "downloads", backup)
        portable_cli(executable, unrelated, cli_home, "backup-info", backup)
        if check_restore:
            saved_hash = hashlib.sha256(backup.read_bytes()).digest()
            other_root = root / 'changed-download-root'
            other_root.mkdir()
            refused = portable_cli(executable, unrelated, cli_home, "backup", "--include-downloads", "--output", backup,
                                   download_root=other_root, expected_exit=1)
            assert b'JMCONSOLE_DOWNLOAD_DIR' in refused.stdout
            assert hashlib.sha256(backup.read_bytes()).digest() == saved_hash
            restored = root / "全新实例"
            portable_cli(executable, unrelated, cli_home, "restore", backup, "--to", restored)
            with closing(sqlite3.connect((restored / "runtime/data/app.db").as_uri() + "?mode=ro", uri=True)) as conn:
                output, = conn.execute("SELECT output_path FROM jobs WHERE job_id='fixture_book'").fetchone()
                assert Path(output) == restored / image.parents[1].relative_to(cli_home)
                assert conn.execute("SELECT status FROM jobs WHERE job_id='fixture_pause'").fetchone() == ('paused',)
                assert conn.execute("PRAGMA user_version").fetchone() == (2,)  # migration belongs to first startup
            assert (restored / image.relative_to(cli_home)).read_bytes() == image.read_bytes()
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
            "portable_backup_info": True, "portable_v2_full_restore": check_restore,
            "unmapped_backup_preserved_old_zip": check_restore,
            "busy_launcher_rejected": launcher_checked, "upstream_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("--check-launcher", action="store_true")
    parser.add_argument("--check-restore", action="store_true", help="offline maintenance; requires a free service mutex")
    options = parser.parse_args()
    print(json.dumps(check(options.release.resolve(), options.check_launcher, options.check_restore)))
