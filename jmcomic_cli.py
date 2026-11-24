#!/usr/bin/env python3
"""
JMComic 命令行工具
用法:
  python jmcomic_cli.py search <关键词>                    # 搜索漫画
  python jmcomic_cli.py info <车号>                        # 查看漫画详情
  python jmcomic_cli.py download <车号>                    # 下载漫画
  python jmcomic_cli.py browse [--category 分类] [--order 排序]  # 分类浏览
  python jmcomic_cli.py batch <车号1,车号2,...>             # 批量下载
"""

import argparse
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import uuid
from jmcomic import JmMagicConstants, JmcomicText
from core import database as db, sdk_adapter
from core.jm_service import close_client


def cmd_backup(args):
    """Offline maintenance never initializes or migrates the source database."""
    from core.backup import create_backup
    from core.instance_lock import maintenance_lock
    from core.path_utils import get_backup_dir, get_download_root
    destination = Path(args.output) if args.output else get_backup_dir() / (
        f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.zip")
    with maintenance_lock():
        result = create_backup(db.DB_PATH, get_download_root(), destination,
                               include_downloads=args.include_downloads,
                               service_stopped=True, timeout=args.timeout)
    resources = result.manifest["resources"]
    print(f"备份已验证并保存：{result.path}")
    print("包含资源文件" if resources["included"] else "仅数据库与资源清单，不含资源文件")
    print(f"数据库版本：{result.manifest['schema_version']}；资源文件数：{len(resources['files'])}")
    return 0


def cmd_backup_info(args):
    from core.backup import inspect_backup
    result = inspect_backup(Path(args.archive), timeout=args.timeout)
    resources = result.manifest["resources"]
    print(f"备份校验通过；数据库版本：{result.manifest['schema_version']}")
    print(f"包含资源文件：{resources['included']}；资源文件数：{len(resources['files'])}")
    return 0


def cmd_restore(args):
    from core.instance_lock import maintenance_lock
    from core.restore import restore_backup
    with maintenance_lock():
        result = restore_backup(Path(args.archive), Path(args.target), timeout=args.timeout)
    print(f"已恢复到新实例：{result.target}")
    print(f"恢复资源文件数：{result.restored_files}")
    for warning in result.warnings:
        print(f"提示：{warning}")
    print("启动该实例前，请将 JMCONSOLE_HOME 设置为上述目录；不会自动移动原目录的数据。")
    print("同时清除或更新 JMCONSOLE_DOWNLOAD_DIR、JMCONSOLE_CACHE_DIR、JMCONSOLE_BACKUP_DIR，避免继续使用旧目录。")
    return 0


def _read_only_settings():
    from core.settings import merge_stored_settings

    try:
        if db.DB_PATH.stat().st_size == 0:
            return merge_stored_settings(())
    except FileNotFoundError:
        return merge_stored_settings(())
    uri = db.DB_PATH.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone()
        if not has_table:
            return merge_stored_settings(())
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
    return merge_stored_settings(rows)


def get_client(*, read_only=True):
    """Use the same saved settings and construction policy as the Web application."""
    if read_only:
        from core.settings import build_jmcomic_option
        option = sdk_adapter.build_option(build_jmcomic_option(_read_only_settings()))
    else:
        db.init_db()
        option = sdk_adapter.build_option()
    client = sdk_adapter.cached_client(option)
    return client, option


def _parse_album_id(raw):
    try:
        return JmcomicText.parse_to_jm_id((raw or "").strip())
    except Exception as error:
        print(f"无效车号：{error}")
        return None


def cmd_search(args):
    """搜索漫画"""
    client, _ = get_client()
    try:
        page = client.search(
            args.keyword,
            page=args.page or 1,
            main_tag=0,
            order_by=JmMagicConstants.ORDER_BY_LATEST,
            time=JmMagicConstants.TIME_ALL,
            category=JmMagicConstants.CATEGORY_ALL,
            sub_category=None,
        )

        albums = list(page.content)
        print(f"搜索 [{args.keyword}] 共 {page.total if hasattr(page,'total') else len(albums)} 条结果:\n")

        for i, (aid, info) in enumerate(albums[:20], 1):
            name = info.get("name", "?") or "?"
            author = info.get("author", "?") or "?"
            tags = ", ".join(info.get("tags", [])[:3]) or "-"
            print(f"  [{i:3d}] {name}")
            print(f"       作者: {author}  |  标签: {tags}  |  车号: {aid}")
            print()

        if len(albums) > 20:
            print(f"  ... 还有 {len(albums) - 20} 条未显示")
        return 0
    finally:
        close_client(client)


def cmd_info(args):
    """查看漫画详情"""
    album_id = _parse_album_id(args.album_id)
    if not album_id:
        return 1
    client, _ = get_client()
    try:
        album = client.get_album_detail(album_id)

        print(f"📖 标题:   {album.name}")
        print(f"✍️ 作者:   {album.author}")
        print(f"🆔 车号:   {album.album_id}")
        print(f"🏷️ 标签:   {', '.join(album.tags) if album.tags else '-'}")
        print(f"🎭 角色:   {', '.join(album.actors) if album.actors else '-'}")
        print(f"📚 作品:   {', '.join(album.works) if album.works else '-'}")
        print(f"👀 观看:   {album.views}  |  ❤️ 点赞: {album.likes}")
        print(f"📄 章节数: {len(album)}")
        print()

        if args.show_chapters is not False:
            print("章节列表:")
            for i, photo in enumerate(album, 1):
                pages = str(len(photo)) if hasattr(photo, '__len__') and hasattr(photo, 'page_arr') and photo.page_arr else "..."
                title = str(photo.name) if hasattr(photo, 'name') and photo.name else "-"
                print(f"  [{i:3d}] {title}  ({pages}页, ID:{photo.photo_id})")
        return 0
    finally:
        close_client(client)


def cmd_download(args):
    """下载漫画"""
    album_id = _parse_album_id(args.album_id)
    if not album_id:
        return 1
    client, option = get_client(read_only=False)
    try:
        print(f"📥 正在下载 {album_id} ...")

        from jmcomic import download_album as jm_download

        album, dler = jm_download(album_id, option=option)
        failed_images = getattr(dler, "download_failed_image", None) or ()
        all_ok = bool(getattr(dler, "all_success", True)) and not failed_images

        if all_ok:
            print(f"\n✅ 下载完成: {album.name}")
        else:
            print(f"\n⚠️ 下载未全部完成: {album.name}")
        print(f"   章节: {len(album)}, 全部成功: {dler.all_success}")

        if failed_images:
            print(f"   ⚠️  {len(failed_images)} 张图片下载失败")

        # 显示下载路径
        if hasattr(dler.option.dir_rule, 'decide_album_root_dir'):
            path = dler.option.dir_rule.decide_album_root_dir(album)
            print(f"   路径: {path}")
        return 0 if all_ok else 1
    finally:
        close_client(client)


def cmd_browse(args):
    """分类浏览"""
    client, _ = get_client()
    try:
        category_map = {
            "all": JmMagicConstants.CATEGORY_ALL,
            "doujin": JmMagicConstants.CATEGORY_DOUJIN,
            "single": JmMagicConstants.CATEGORY_SINGLE,
            "short": JmMagicConstants.CATEGORY_SHORT,
            "hanman": JmMagicConstants.CATEGORY_HANMAN,
            "meiman": JmMagicConstants.CATEGORY_MEIMAN,
        }

        order_map = {
            "latest": JmMagicConstants.ORDER_BY_LATEST,
            "views": JmMagicConstants.ORDER_BY_VIEW,
            "likes": JmMagicConstants.ORDER_BY_LIKE,
            "pictures": JmMagicConstants.ORDER_BY_PICTURE,
        }

        time_map = {
            "all": JmMagicConstants.TIME_ALL,
            "today": JmMagicConstants.TIME_TODAY,
            "week": JmMagicConstants.TIME_WEEK,
            "month": JmMagicConstants.TIME_MONTH,
        }

        cat = category_map.get(args.category, JmMagicConstants.CATEGORY_ALL)
        order = order_map.get(args.order, JmMagicConstants.ORDER_BY_LATEST)
        time_range = time_map.get(args.time, JmMagicConstants.TIME_ALL)

        page = client.categories_filter(
            page=args.page or 1,
            time=time_range,
            category=cat,
            order_by=order,
            sub_category=None,
        )

        albums = list(page.content)
        title = f"分类浏览: {args.category} / {args.order} / {args.time}"
        print(f"{title} (共 {page.total if hasattr(page,'total') else len(albums)} 条):\n")

        for i, (aid, info) in enumerate(albums[:20], 1):
            name = info.get("name", "?") or "?"
            author = info.get("author", "?") or "?"
            likes = info.get("likes", "?")
            print(f"  [{i:3d}] {name}")
            print(f"       作者: {author}  |  ❤️ {likes}  |  车号: {aid}")
            print()
        return 0
    finally:
        close_client(client)


def cmd_batch(args):
    """批量下载多个漫画"""
    ids = []
    for value in args.ids.split(","):
        token = value.strip()
        if not token:
            continue
        parsed = _parse_album_id(token)
        if not parsed:
            return 1
        if parsed not in ids:
            ids.append(parsed)
    if not ids:
        print("批量下载至少需要一个车号。")
        return 1

    print(f"📥 批量下载 {len(ids)} 个漫画: {', '.join(ids)}")

    from jmcomic import download_batch, download_album

    client, option = get_client(read_only=False)
    try:
        results = download_batch(download_album, ids, option=option)
        if results.failed or len(results) != len(ids):
            print(f"\n批量下载未全部完成: {len(results)}/{len(ids)}")
            if results.failed:
                print(f"失败车号: {', '.join(results.failed)}")
            return 1
        print(f"\n✅ 批量下载完成: {len(results)}/{len(ids)}")
        return 0
    finally:
        close_client(client)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="JMComic 命令行工具 - 搜索/浏览/下载禁漫漫画",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # search
    p_search = sub.add_parser("search", help="搜索漫画")
    p_search.add_argument("keyword", help="搜索关键词")
    p_search.add_argument("--page", type=int, default=1, help="页码")

    # info
    p_info = sub.add_parser("info", help="查看漫画详情")
    p_info.add_argument("album_id", help="禁漫车号")
    p_info.add_argument("--no-chapters", dest="show_chapters", action="store_false", help="不显示章节列表")

    # download
    p_dl = sub.add_parser("download", help="下载漫画")
    p_dl.add_argument("album_id", help="禁漫车号")

    # browse
    p_browse = sub.add_parser("browse", help="分类浏览")
    p_browse.add_argument("--category", default="all", choices=["all","doujin","single","short","hanman","meiman"], help="分类")
    p_browse.add_argument("--order", default="latest", choices=["latest","views","likes","pictures"], help="排序")
    p_browse.add_argument("--time", default="all", choices=["all","today","week","month"], help="时间范围")
    p_browse.add_argument("--page", type=int, default=1, help="页码")

    # batch
    p_batch = sub.add_parser("batch", help="批量下载")
    p_batch.add_argument("ids", help="车号列表，逗号分隔 (如 123,456,789)")

    p_backup = sub.add_parser("backup", help="停止程序后备份数据库及资源清单，可选包含资源文件")
    p_backup.add_argument("--output", help="备份ZIP路径，默认写入配置的备份目录")
    p_backup.add_argument("--include-downloads", action="store_true", help="备份中包含下载文件")
    p_backup.add_argument("--timeout", type=float, default=120, help="总时间预算（秒）")
    p_info = sub.add_parser("backup-info", help="验证备份完整性并显示摘要")
    p_info.add_argument("archive", help="备份ZIP路径")
    p_info.add_argument("--timeout", type=float, default=120)
    p_restore = sub.add_parser("restore", help="验证并恢复到全新实例目录，不覆盖现有实例")
    p_restore.add_argument("archive", help="备份ZIP路径")
    p_restore.add_argument("--to", dest="target", required=True, help="不存在的绝对目标目录")
    p_restore.add_argument("--timeout", type=float, default=120)

    args = parser.parse_args(argv)

    if args.command in {"backup", "backup-info", "restore"}:
        from core.backup import BackupError
        from core.restore import RestoreError
        from core.instance_lock import InstanceBusyError
        try:
            return {"backup": cmd_backup, "backup-info": cmd_backup_info,
                    "restore": cmd_restore}[args.command](args)
        except (BackupError, RestoreError, InstanceBusyError) as error:
            print(f"操作未完成：{error}")
            return 1

    if args.command == "search":
        return cmd_search(args)
    elif args.command == "info":
        return cmd_info(args)
    elif args.command in {"download", "batch"}:
        # SDK top-level downloads also write resource files, even though they do
        # not create Web queue rows. Exclude them during maintenance/serving too.
        from core.instance_lock import maintenance_lock, InstanceBusyError
        try:
            with maintenance_lock():
                return {"download": cmd_download, "batch": cmd_batch}[args.command](args)
        except InstanceBusyError as error:
            print(f"操作未完成：{error}")
            return 1
    elif args.command == "browse":
        return cmd_browse(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
