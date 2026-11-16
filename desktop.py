"""Portable Windows entry: launcher, owned server, maintenance and isolated checks.

Keep business imports inside dispatch: self-check must choose disposable paths
before any module captures the instance configuration.
"""
import argparse
import json
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "_lifecycle-worker":
        if len(args) != 3:
            raise ValueError("invalid lifecycle worker arguments")
        from core.release_lifecycle import run_worker
        return run_worker(args[1], args[2])
    if args and args[0] == "cli":
        from jmcomic_cli import main as cli_main
        return cli_main(args[1:]) or 0
    parser = argparse.ArgumentParser(description="JMComic Windows portable console")
    parser.add_argument("command", nargs="?", choices=("launch", "serve", "self-check", "lifecycle-check"), default="launch",
                        help="默认打开本地界面；self-check 使用临时数据离线自检；cli 提供维护命令")
    options = parser.parse_args(args)
    if options.command == "lifecycle-check":
        from core.release_lifecycle import run_lifecycle_check
        result = run_lifecycle_check()
        print(json.dumps(result, ensure_ascii=True))
        return result["exit_code"]
    if options.command == "self-check":
        from core.release_check import run_self_check
        result = run_self_check()
        print(json.dumps(result, ensure_ascii=True))
        return result["exit_code"]
    if options.command == "serve":
        from app import main as serve
        return serve()
    from launcher import main as launch
    return launch(["--wait"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Startup failed: {error}", file=sys.stderr)
        raise SystemExit(1)
