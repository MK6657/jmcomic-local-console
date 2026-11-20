"""Portable Windows entry: launcher, owned server, maintenance and isolated checks.

Keep business imports inside dispatch: self-check must choose disposable paths
before any module captures the instance configuration.
"""
import argparse
import json
import os
import sys


def _instance_command(command, home):
    from core.path_utils import get_path_config

    paths = get_path_config() if home is None else get_path_config(dict(os.environ, JMCONSOLE_HOME=home))
    database = paths.data / "app.db"
    if command == "instance" or home is not None:
        print(f"实例目录：{paths.home}\n数据库：{database}\n下载目录：{paths.downloads}\n"
              f"缓存目录：{paths.cache}\n备份目录：{paths.backups}\n日志目录：{paths.logs}")
    if command == "instance":
        print('以上是启动时将使用的位置。选择既有实例：JMComic.exe launch --home "实例绝对目录"。\n'
              '仅打开已有服务：JMComic.exe attach；也可指定 --home。此次查看不会启动服务。')
        return 0
    if home is not None:
        if command == "launch":
            try:
                confirmed = input("使用以上目录启动？旧库可能随启动升级。输入 y 确认，直接回车取消：").strip().lower() == "y"
            except (EOFError, KeyboardInterrupt):
                confirmed = False
            if not confirmed:
                print("已取消，原实例和启动设置保持原样。")
                return 0
        if not database.is_file() or not database.stat().st_size:
            raise ValueError("所选目录没有既有实例的 runtime/data/app.db；请核对实例根目录。未创建或迁移数据。")
        if paths != get_path_config():
            raise RuntimeError("本进程已加载其他实例的目录配置，请从新窗口运行所选命令。")
    elif command == "launch":
        print(f"实例目录：{paths.home}\n查看全部位置：JMComic.exe instance；仅打开已有服务：JMComic.exe attach。")
    from launcher import main as launch
    return launch(["--attach"] if command == "attach" else ["--wait"])


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
    if args and args[0] == 'demo':
        demo = argparse.ArgumentParser(description='打开独立的离线藏书阁样书实例')
        demo.add_argument('--no-browser', action='store_true', help='只输出预览地址')
        options = demo.parse_args(args[1:])
        from core.atelier_demo import run_demo
        return run_demo(open_browser=not options.no_browser)
    parser = argparse.ArgumentParser(description="JMComic Windows portable console")
    parser.add_argument("command", nargs="?", choices=("launch", "instance", "attach", "serve", "self-check", "lifecycle-check"),
                        default="launch", help="launch 启动；instance 只查看目录；attach 只打开既有服务；cli 提供维护命令")
    parser.add_argument("--home", metavar="绝对目录", help="仅本次选择实例根目录；其他存储目录仍按环境设置。launch/attach 须指向既有实例")
    options = parser.parse_args(args)
    if options.home is not None and options.command not in ("instance", "launch", "attach"):
        parser.error("--home 仅用于 instance、launch 或 attach")
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
        from core.instance_lock import acquire_instance_lock
        lease = acquire_instance_lock()
        try:
            from app import main as serve
        except BaseException:
            lease.close()
            raise
        return serve(instance_lease=lease)
    if options.home is None:
        return _instance_command(options.command, options.home)
    previous_home = os.environ.get("JMCONSOLE_HOME")
    try:
        os.environ["JMCONSOLE_HOME"] = options.home
        return _instance_command(options.command, options.home)
    finally:
        if previous_home is None:
            del os.environ["JMCONSOLE_HOME"]
        else:
            os.environ["JMCONSOLE_HOME"] = previous_home


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Startup failed: {error}", file=sys.stderr)
        raise SystemExit(1)
