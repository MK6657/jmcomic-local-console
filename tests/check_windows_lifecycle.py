"""Explicit local lifecycle smoke check; excluded from ordinary pytest collection.

Run with source Python, or pass --executable to check an already-built portable EXE.
No ordinary instance is started/stopped; the command owns disposable workers only.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args(argv)
    command = ([str(args.executable.resolve())] if args.executable else
               [sys.executable, str(Path(__file__).resolve().parent.parent / "desktop.py")])
    result = subprocess.run([*command, "lifecycle-check"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=150)
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError("生命周期自检没有返回JSON结果") from None
    print(json.dumps(report, ensure_ascii=True))
    return 0 if result.returncode == 0 and report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
