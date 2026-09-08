#!/usr/bin/env python3
"""
JMComic 命令行工具
用法:
  python jmcomic_cli.py search <关键词>                    # 搜索漫画
  python jmcomic_cli.py info <车号>                        # 查看漫画详情
  python jmcomic_cli.py download <车号> [章节范围]          # 下载漫画
  python jmcomic_cli.py browse [--category 分类] [--order 排序]  # 分类浏览
  python jmcomic_cli.py batch <车号1,车号2,...>             # 批量下载
"""

import sys
import os
import argparse
from pathlib import Path
from jmcomic import *
from core.jm_service import close_client

# 下载目录 —— 与 Web 应用保持一致（项目 downloads/ 目录）
from core.path_guard import DOWNLOAD_ROOT


def get_client():
    option = JmModuleConfig.option_class().default()
    client = option.build_jm_client()
    return client, option


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
    finally:
        close_client(client)


def cmd_info(args):
    """查看漫画详情"""
    client, _ = get_client()
    try:
        album = client.get_album_detail(args.album_id)

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
    finally:
        close_client(client)


def cmd_download(args):
    """下载漫画"""
    client, option = get_client()
    try:
        print(f"📥 正在下载 {args.album_id} ...")

        from jmcomic import download_album as jm_download

        album, dler = jm_download(args.album_id, option=option)

        print(f"\n✅ 下载完成: {album.name}")
        print(f"   章节: {len(album)}, 全部成功: {dler.all_success}")

        if dler.download_failed_image:
            print(f"   ⚠️  {len(dler.download_failed_image)} 张图片下载失败")

        # 显示下载路径
        if hasattr(dler.option.dir_rule, 'decide_album_root_dir'):
            path = dler.option.dir_rule.decide_album_root_dir(album)
            print(f"   路径: {path}")
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
    finally:
        close_client(client)


def cmd_batch(args):
    """批量下载多个漫画"""
    ids = args.ids.split(",")
    ids = [i.strip() for i in ids if i.strip()]

    print(f"📥 批量下载 {len(ids)} 个漫画: {', '.join(ids)}")

    from jmcomic import download_batch, download_album

    results = download_batch(download_album, ids)
    print(f"\n✅ 批量下载完成: {len(results)}/{len(ids)}")


def main():
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

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "info":
        cmd_info(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "browse":
        cmd_browse(args)
    elif args.command == "batch":
        cmd_batch(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
