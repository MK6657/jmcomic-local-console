"""初始化「藏书阁 · Atelier Edition」的专用、有归属标识的演示实例。

绝不触碰外层仓库，也不污染默认的 atelier-edition/runtime 与 atelier-edition/downloads。
全部通过本地 Pillow + SQLite v5 + publication 快照契约离线生成，零网络请求。

生成的 6 部合成典藏样书覆盖全站 12 个路由与全部核心业务状态：
  1. 901001《海岸慢车》（水野苍）— 同书聚合标杆：第1话 paused(8/20P 含真实断点回执) + 第2话 completed(12/12P 立即可读)
     + album_maintenance pending（等待第1话完结后合卷）+ 1/3 话局部下载证明 + 已确认 1 话新章节
  2. 901002《松林雾山纪行》（青木岚）— 双章完整散图可读（16P）+ 收藏 + 新章节检查无更新
  3. 901003《雨夜钟楼与旧书店》（白石夜）— 纯 CBZ 归档直读标杆（12P 打包为 .cbz，无散图，含任务快照）
  4. 901004《本草植物标本集》（林间鹿）— 本地散图可读（10P）+ album_maintenance failed（支持离线一键「重新整理整册」）+ 1 话新章节
  5. 901005《山野来信 · 铁道速写帖》（高桥旬）— 第1话 failed(5/18P) + 第2话 canceled(3/16P) 任务记录标杆
  6. 901006《白麻画室与冬日茶席》（佐藤堇）— 仅收藏 · 未下载（wishlist none）对比标杆
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import stat
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

ATELIER_ROOT = Path(__file__).resolve().parent.parent
OUTER_ROOT = ATELIER_ROOT.parent
DEFAULT_DEMO_HOME = ATELIER_ROOT / "_atelier_preview"
OWNER_FILE = ".atelier-demo-owner-v1"
READY_FILE = ".atelier-demo-ready-v1"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成藏书阁 Atelier Edition 隔离演示种子库")
    parser.add_argument(
        "--home",
        default="",
        help="指定新的绝对目录，或此脚本已创建的实例。默认 _atelier_preview，忽略环境中的实例目录。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--keep', dest='keep', action='store_true', help='复用完整的演示库（默认）')
    mode.add_argument('--rebuild', dest='keep', action='store_false', help='重新生成已确认归属的演示库及样书')
    parser.set_defaults(keep=True)
    return parser.parse_args(argv)


def _check_entry(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"拒绝链接或重解析点：{path}")
    if not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ValueError(f"拒绝特殊文件或硬链接：{path}")
    return info


def _resolve_demo_home(explicit_home: str | Path) -> Path:
    path = Path(explicit_home) if explicit_home else DEFAULT_DEMO_HOME
    if not path.is_absolute() or path == Path(path.anchor) or '..' in path.parts:
        raise ValueError("演示目录必须是明确的绝对子目录")
    if os.name == 'nt' and (path.drive.startswith('\\\\') or any(
        part.endswith((' ', '.')) or ':' in part or Path(part).is_reserved() for part in path.parts[1:]
    )):
        raise ValueError("演示目录必须使用本机普通目录名")
    for parent in reversed(path.parents):
        if not stat.S_ISDIR(_check_entry(parent).st_mode):
            raise ValueError(f"不是目录：{parent}")
    try:
        _check_entry(path)
    except FileNotFoundError:
        pass
    target = path.resolve()
    outer_checkout = not getattr(sys, 'frozen', False) and (OUTER_ROOT / 'core/path_utils.py').is_file()
    if (target in (ATELIER_ROOT, *ATELIER_ROOT.parents) or target.is_relative_to(ATELIER_ROOT / '_demo_home')
            or (target.is_relative_to(ATELIER_ROOT) and target != DEFAULT_DEMO_HOME)
            or (outer_checkout and target.is_relative_to(OUTER_ROOT) and target != DEFAULT_DEMO_HOME)):
        raise ValueError("拒绝在源码或原有演示实例中写入；请指定新的专用目录")
    return target


def _prepare_home(home: Path) -> bool:
    try:
        home.mkdir(mode=0o700)
    except FileExistsError:
        created = False
    else:
        created = True
    info = _check_entry(home)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("所选位置不是目录")
    marker = home / OWNER_FILE
    identity = json.dumps(['jmconsole-atelier-demo-v1', os.path.normcase(str(home)), info.st_dev, info.st_ino]).encode('ascii')
    if created:
        with marker.open('xb') as stream:
            stream.write(identity)
        return True
    try:
        marked = _check_entry(marker)
    except FileNotFoundError:
        raise ValueError("所选目录已存在且不是本脚本创建的演示实例；未修改任何内容") from None
    if not stat.S_ISREG(marked.st_mode) or marked.st_size != len(identity) or marker.read_bytes() != identity:
        raise ValueError("演示目录归属标识不匹配；未修改任何内容")
    pending = [home]
    while pending:
        for child in pending.pop().iterdir():
            if stat.S_ISDIR(_check_entry(child).st_mode):
                pending.append(child)
    return False


def _load_font(size: int, *, serif: bool = False, mono: bool = False):
    from PIL import ImageFont

    win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if mono:
        candidates = ["consola.ttf", "CascadiaMono.ttf", "cour.ttf", "msyh.ttc"]
    elif serif:
        candidates = ["STSONG.TTF", "simsun.ttc", "simkai.ttf", "msyh.ttc"]
    else:
        candidates = ["msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"]
    for name in candidates:
        font_path = win_fonts / name
        if font_path.is_file():
            try:
                return ImageFont.truetype(str(font_path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_motif(draw, motif: str, box: tuple[int, int, int, int], palette: dict[str, str]) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    bg = palette["art_bg"]
    primary = palette["primary"]
    secondary = palette["secondary"]
    accent = palette["accent"]
    paper = palette["paper"]

    draw.rectangle(box, fill=bg)

    if motif == "coastal_train":
        # 暮色圆日 + 远海层浪 + 灯塔光束 + 海岸高架慢车
        sun_r = int(w * 0.19)
        cx, cy = x0 + int(w * 0.70), y0 + int(h * 0.30)
        draw.ellipse((cx - sun_r, cy - sun_r, cx + sun_r, cy + sun_r), fill=accent)
        # 灯塔光束
        lx, ly = x0 + int(w * 0.24), y0 + int(h * 0.34)
        draw.polygon([(lx, ly), (x1, y0 + int(h * 0.16)), (x1, y0 + int(h * 0.44))], fill=palette["gleam"])
        # 礁石与灯塔
        draw.polygon(
            [(x0, y0 + int(h * 0.66)), (x0 + int(w * 0.36), y0 + int(h * 0.66)), (x0 + int(w * 0.28), y0 + int(h * 0.50)), (x0, y0 + int(h * 0.56))],
            fill=primary,
        )
        draw.rectangle((lx - 11, ly, lx + 11, y0 + int(h * 0.54)), fill=paper, outline=primary, width=2)
        draw.rectangle((lx - 15, ly - 8, lx + 15, ly + 2), fill=secondary)
        # 海面波纹
        for i in range(6):
            wy = y0 + int(h * 0.66) + i * int(h * 0.045)
            draw.rectangle((x0, wy, x1, wy + int(h * 0.025)), fill=primary if i % 2 == 0 else secondary)
            for k in range(4):
                sx = x0 + int(w * (0.14 + k * 0.22 + (i % 2) * 0.06))
                draw.line((sx, wy + 4, sx + 48, wy + 4), fill=paper, width=2)
        # 海岸栈桥与慢车车厢
        bridge_y = y0 + int(h * 0.78)
        draw.rectangle((x0, bridge_y, x1, bridge_y + 10), fill=palette["ink"])
        for px in range(x0 + 24, x1, 56):
            draw.rectangle((px, bridge_y + 10, px + 10, y1), fill=palette["ink"])
        for car in range(3):
            cx0 = x0 + 46 + car * 118
            cy0 = bridge_y - 34
            draw.rounded_rectangle((cx0, cy0, cx0 + 104, bridge_y - 3), radius=5, fill=secondary, outline=palette["ink"], width=2)
            for win in range(4):
                wx0 = cx0 + 10 + win * 22
                draw.rectangle((wx0, cy0 + 8, wx0 + 15, cy0 + 20), fill=paper)

    elif motif == "misty_pine":
        # 宋韵粉青层峦叠嶂 + 云雾留白 + 苍松剪影
        layers = [
            (0.32, secondary, 0.08),
            (0.48, primary, 0.11),
            (0.66, palette["ink"], 0.09),
        ]
        for base_ratio, color, amp in layers:
            pts = [(x0, y1)]
            for step in range(0, 25):
                px = x0 + int(w * step / 24)
                py = y0 + int(h * (base_ratio - amp * math.sin(step * 0.55 + base_ratio * 6)))
                pts.append((px, py))
            pts.append((x1, y1))
            draw.polygon(pts, fill=color)
            # 山间云带
            band_y = y0 + int(h * (base_ratio + 0.05))
            draw.rectangle((x0, band_y, x1, band_y + int(h * 0.045)), fill=paper)
        # 朱砂远日
        r = int(w * 0.11)
        draw.ellipse((x0 + int(w * 0.66) - r, y0 + int(h * 0.22) - r, x0 + int(w * 0.66) + r, y0 + int(h * 0.22) + r), fill=accent)
        # 近景松枝
        draw.line((x0 + 16, y1 - 30, x0 + int(w * 0.44), y0 + int(h * 0.56)), fill=palette["ink"], width=7)
        for nx, ny, nr in [(0.34, 0.58, 34), (0.44, 0.53, 30), (0.25, 0.63, 36)]:
            ccx, ccy = x0 + int(w * nx), y0 + int(h * ny)
            draw.pieslice((ccx - nr, ccy - nr, ccx + nr, ccy + nr), 180, 360, fill=primary)

    elif motif == "clocktower_night":
        # 雨夜钟楼 + 鎏金弦月 + 旧书店暖窗 + 细雨斜丝
        mr = int(w * 0.13)
        mx, my = x0 + int(w * 0.26), y0 + int(h * 0.22)
        draw.ellipse((mx - mr, my - mr, mx + mr, my + mr), fill=accent)
        draw.ellipse((mx - mr + 18, my - mr - 8, mx + mr + 18, my + mr - 8), fill=bg)
        # 钟楼剪影
        tx0, tx1 = x0 + int(w * 0.52), x0 + int(w * 0.80)
        ty_top = y0 + int(h * 0.18)
        draw.polygon([(tx0 + (tx1 - tx0) // 2, ty_top - 48), (tx0 - 6, ty_top + 24), (tx1 + 6, ty_top + 24)], fill=primary)
        draw.rectangle((tx0, ty_top + 24, tx1, y1), fill=primary)
        dial_r = int((tx1 - tx0) * 0.30)
        dcx, dcy = (tx0 + tx1) // 2, ty_top + 86
        draw.ellipse((dcx - dial_r, dcy - dial_r, dcx + dial_r, dcy + dial_r), fill=paper, outline=accent, width=3)
        draw.line((dcx, dcy, dcx, dcy - dial_r + 10), fill=bg, width=3)
        draw.line((dcx, dcy, dcx + dial_r - 12, dcy + 6), fill=bg, width=2)
        # 旧书店屋檐与暖光橱窗
        shop_y = y0 + int(h * 0.64)
        draw.rectangle((x0 + 28, shop_y, tx0 + 18, y1), fill=secondary)
        for wx in range(x0 + 50, tx0 - 20, 62):
            draw.rectangle((wx, shop_y + 34, wx + 44, shop_y + 98), fill=accent, outline=bg, width=2)
        # 斜雨丝
        for rx in range(x0 - 40, x1 + 40, 28):
            draw.line((rx + 36, y0 + 8, rx - 18, y1 - 8), fill=palette["gleam"], width=1)

    elif motif == "botanical_folio":
        # 本草标本铜版画框 + 银杏扇叶与蕨叶几何枝干
        pad = 24
        draw.rectangle((x0 + pad, y0 + pad, x1 - pad, y1 - pad), outline=primary, width=2)
        draw.rectangle((x0 + pad + 6, y0 + pad + 6, x1 - pad - 6, y1 - pad - 6), outline=secondary, width=1)
        cx, stem_bottom = (x0 + x1) // 2, y1 - 54
        draw.line((cx, stem_bottom, cx, y0 + int(h * 0.28)), fill=primary, width=4)
        # 银杏扇叶簇
        fan_specs = [
            (cx, y0 + int(h * 0.30), 76, 215, 325, accent),
            (cx - 56, y0 + int(h * 0.46), 66, 195, 295, primary),
            (cx + 56, y0 + int(h * 0.46), 66, 245, 345, primary),
            (cx - 44, y0 + int(h * 0.64), 58, 190, 285, secondary),
            (cx + 44, y0 + int(h * 0.64), 58, 255, 350, secondary),
        ]
        for fx, fy, fr, a0, a1, col in fan_specs:
            draw.line((cx, min(stem_bottom - 20, fy + 46), fx, fy), fill=primary, width=2)
            draw.pieslice((fx - fr, fy - fr, fx + fr, fy + fr), a0, a1, fill=col)

    elif motif == "mountain_post":
        # 峡谷木拱桥 + 远山几何色块 + 邮政戳记
        draw.polygon([(x0, y0 + int(h * 0.58)), (x0 + int(w * 0.42), y0 + int(h * 0.22)), (x0 + int(w * 0.82), y0 + int(h * 0.58))], fill=secondary)
        draw.polygon([(x0 + int(w * 0.28), y0 + int(h * 0.64)), (x0 + int(w * 0.72), y0 + int(h * 0.28)), (x1, y0 + int(h * 0.64))], fill=primary)
        arch_y = y0 + int(h * 0.62)
        draw.rectangle((x0, arch_y, x1, arch_y + 14), fill=palette["ink"])
        draw.arc((x0 + 42, arch_y + 6, x1 - 42, y1 + 60), 180, 360, fill=palette["ink"], width=8)
        for px in range(x0 + 48, x1 - 36, 38):
            draw.line((px, arch_y + 14, px, y1 - 24), fill=accent, width=3)
        # 邮戳圆环
        sr = 38
        sx, sy = x1 - 74, y0 + 72
        draw.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), outline=accent, width=3)
        draw.ellipse((sx - sr + 7, sy - sr + 7, sx + sr - 7, sy + sr - 7), outline=accent, width=1)

    else:  # tea_atelier
        # 白麻画室窗格 + 宋瓷茶碗与冬梅斜枝
        for gx in range(1, 4):
            lx = x0 + int(w * gx / 4)
            draw.line((lx, y0, lx, y0 + int(h * 0.62)), fill=secondary, width=2)
        draw.line((x0, y0 + int(h * 0.32), x1, y0 + int(h * 0.32)), fill=secondary, width=2)
        draw.rectangle((x0, y0 + int(h * 0.62), x1, y1), fill=palette["gleam"])
        # 茶碗器型
        bx0, bx1 = x0 + int(w * 0.30), x0 + int(w * 0.70)
        by0, by1 = y0 + int(h * 0.54), y0 + int(h * 0.78)
        draw.pieslice((bx0, by0 - 36, bx1, by1), 0, 180, fill=primary)
        draw.rectangle((bx0 + int((bx1 - bx0) * 0.32), by1 - 6, bx0 + int((bx1 - bx0) * 0.68), by1 + 10), fill=palette["ink"])
        # 斜梅枝与朱砂花苞
        draw.line((x1 - 18, y0 + 42, x0 + int(w * 0.34), y0 + int(h * 0.40)), fill=palette["ink"], width=5)
        for bx, by in [(0.72, 0.20), (0.58, 0.27), (0.45, 0.34), (0.64, 0.34)]:
            px, py = x0 + int(w * bx), y0 + int(h * by)
            draw.ellipse((px - 10, py - 10, px + 10, py + 10), fill=accent)


def render_cover_bytes(book: dict) -> bytes:
    """绘制 720×960 (3:4) 东方木版画/书籍装帧质感封面 WebP。"""
    from PIL import Image, ImageDraw

    w, h = 720, 960
    pal = book["palette"]
    img = Image.new("RGB", (w, h), pal["paper"])
    draw = ImageDraw.Draw(img)

    # 外框细装帧线与左侧装订压痕
    draw.rectangle((26, 26, w - 26, h - 26), outline=pal["primary"], width=2)
    draw.rectangle((34, 34, w - 34, h - 34), outline=pal["secondary"], width=1)
    draw.rectangle((0, 0, 16, h), fill=pal["primary"])

    # 主插画版芯
    art_box = (58, 118, w - 58, 676)
    _draw_motif(draw, book["motif"], art_box, pal)
    draw.rectangle(art_box, outline=pal["ink"], width=2)

    font_kicker = _load_font(20, mono=True)
    font_title = _load_font(44, serif=True)
    font_meta = _load_font(24, serif=False)
    font_seal = _load_font(24, serif=True)

    # 顶栏典藏编号铭文
    draw.text((60, 62), f"ATELIER EDITION  ·  JM-{book['album_id']}", fill=pal["primary"], font=font_kicker)

    # 右上朱文方印「藏」
    seal_box = (w - 110, 52, w - 60, 102)
    draw.rounded_rectangle(seal_box, radius=4, fill=pal["seal"])
    draw.text((w - 97, 61), "藏", fill=pal["paper"], font=font_seal)

    # 底部书名签条与著者署名
    draw.text((60, 712), book["title"], fill=pal["ink"], font=font_title)
    draw.line((60, 782, w - 60, 782), fill=pal["secondary"], width=2)
    draw.text((60, 804), f"著者 · {book['author']}", fill=pal["primary"], font=font_meta)
    draw.text((60, 848), book["subtitle"], fill=pal["secondary"], font=font_meta)
    draw.text((w - 220, 888), f"FOLIO #{book['album_id']}", fill=pal["primary"], font=font_kicker)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90)
    return buf.getvalue()


def render_story_page_bytes(book: dict, chapter_title: str, page_no: int, total_pages: int) -> bytes:
    """绘制 900×1280 漫画分镜阅读内页（第 1 页直接采用全幅精装彩扉，后续页为分镜版画画稿）。"""
    if page_no == 1:
        return render_cover_bytes(book)

    from PIL import Image, ImageDraw

    w, h = 900, 1280
    pal = book["palette"]
    img = Image.new("RGB", (w, h), pal["paper"])
    draw = ImageDraw.Draw(img)

    font_head = _load_font(20, mono=True)
    font_chap = _load_font(28, serif=True)
    font_caption = _load_font(22, serif=False)

    # 页眉书眉线
    draw.text((68, 44), f"{book['title']}  ·  {chapter_title}", fill=pal["primary"], font=font_chap)
    draw.text((w - 230, 50), f"FOLIO {page_no:03d} / {total_pages:03d}", fill=pal["secondary"], font=font_head)
    draw.line((68, 92, w - 68, 92), fill=pal["secondary"], width=2)

    # 三格漫画分镜构图（随页码错落变化）
    variant = page_no % 3
    if variant == 0:
        panels = [(68, 122, w - 68, 520), (68, 546, 448, 1080), (474, 546, w - 68, 1080)]
    elif variant == 1:
        panels = [(68, 122, 468, 610), (494, 122, w - 68, 610), (68, 636, w - 68, 1080)]
    else:
        panels = [(68, 122, w - 68, 440), (68, 466, w - 68, 780), (68, 806, w - 68, 1080)]

    for idx, pbox in enumerate(panels):
        _draw_motif(draw, book["motif"], pbox, pal)
        draw.rectangle(pbox, outline=pal["ink"], width=3)
        # 对白竖签框
        bx0, by0 = pbox[0] + 22, pbox[1] + 22
        draw.rounded_rectangle((bx0, by0, bx0 + 236, by0 + 52), radius=8, fill=pal["paper"], outline=pal["primary"], width=2)
        draw.text((bx0 + 16, by0 + 11), f"SCENE {page_no:02d}-{idx + 1} · 画格手稿", fill=pal["ink"], font=font_caption)

    # 页脚书口码
    draw.line((68, h - 96, w - 68, h - 96), fill=pal["secondary"], width=1)
    draw.text((68, h - 74), f"藏书阁 · ATELIER ARCHIVE  |  {book['author']}", fill=pal["secondary"], font=font_head)
    draw.text((w - 164, h - 74), f"— {page_no:02d} —", fill=pal["ink"], font=font_head)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=86)
    return buf.getvalue()


DEMO_BOOKS = [
    {
        "album_id": "901001",
        "title": "海岸慢车",
        "author": "水野苍",
        "subtitle": "潮汐铁道与暮色灯塔短篇连载集",
        "description": "沿着半岛西岸行驶的单节慢车，记录起点站、北岬灯塔与潮汐时刻表之间的旅人书信。",
        "motif": "coastal_train",
        "views": 18420,
        "likes": 3290,
        "wishlist": "completed",
        "auto_tags": ["旅行", "治愈", "风景", "全彩", "连载"],
        "custom_tags": ["典藏必读", "水野苍"],
        "palette": {
            "paper": "#F6F1E7",
            "art_bg": "#DCE8EC",
            "primary": "#215266",
            "secondary": "#5D7E8A",
            "accent": "#C97A42",
            "gleam": "#EFE6D5",
            "ink": "#24201B",
            "seal": "#8F2D22",
        },
        "chapters": [
            {"photo_id": "90100101", "title": "第01话 · 起点站", "page_count": 20},
            {"photo_id": "90100102", "title": "第02话 · 灯塔以北", "page_count": 12},
            {"photo_id": "90100103", "title": "第03话 · 潮汐时刻表", "page_count": 16},
        ],
    },
    {
        "album_id": "901002",
        "title": "松林雾山纪行",
        "author": "青木岚",
        "subtitle": "宋韵粉青山水写生与古道踏查录",
        "description": "穿行于海拔两千米的云雾松林，以淡彩与木刻笔触描摹山间雨声、石阶与听松亭。",
        "motif": "misty_pine",
        "views": 12650,
        "likes": 2410,
        "wishlist": "completed",
        "auto_tags": ["山水", "纪行", "水墨", "青年", "完结"],
        "custom_tags": ["睡前慢读", "山水名品"],
        "palette": {
            "paper": "#EEF3EF",
            "art_bg": "#DAE6DF",
            "primary": "#1F5647",
            "secondary": "#587B6F",
            "accent": "#B3563D",
            "gleam": "#E7EFEA",
            "ink": "#1E2621",
            "seal": "#8F2D22",
        },
        "chapters": [
            {"photo_id": "90100201", "title": "第01话 · 入山雾", "page_count": 8},
            {"photo_id": "90100202", "title": "第02话 · 听松亭", "page_count": 8},
        ],
    },
    {
        "album_id": "901003",
        "title": "雨夜钟楼与旧书店",
        "author": "白石夜",
        "subtitle": "旧街钟摆、古籍修补匠与午夜避雨客",
        "description": "每逢连绵夜雨便会亮起琥珀暖灯的街角旧书店，收录装订成 CBZ 典藏卷的两则奇谭。",
        "motif": "clocktower_night",
        "views": 24910,
        "likes": 4680,
        "wishlist": "completed",
        "auto_tags": ["悬疑", "奇幻", "日常", "短篇"],
        "custom_tags": ["cbz归档", "暗房夜读"],
        "palette": {
            "paper": "#F3EFE7",
            "art_bg": "#242A32",
            "primary": "#384A5E",
            "secondary": "#5B6E82",
            "accent": "#DEAB62",
            "gleam": "#495B70",
            "ink": "#21201D",
            "seal": "#8F2D22",
        },
        "chapters": [
            {"photo_id": "90100301", "title": "第01话 · 避雨人", "page_count": 6},
            {"photo_id": "90100302", "title": "第02话 · 午夜钟摆", "page_count": 6},
        ],
    },
    {
        "album_id": "901004",
        "title": "本草植物标本集",
        "author": "林间鹿",
        "subtitle": "四季叶脉拓印与山野植物铜版图谱",
        "description": "以手工棉纸压制的银杏、蕨类与山茶标本图鉴，附采集地经纬、节气物候与染色配方。",
        "motif": "botanical_folio",
        "views": 9830,
        "likes": 1920,
        "wishlist": None,
        "auto_tags": ["图鉴", "自然", "科普", "全彩"],
        "custom_tags": ["装帧参考", "典藏必读"],
        "palette": {
            "paper": "#F7F4EC",
            "art_bg": "#ECE6D8",
            "primary": "#3A5A40",
            "secondary": "#7A8B6E",
            "accent": "#B67D38",
            "gleam": "#F3EFE4",
            "ink": "#24201B",
            "seal": "#8F2D22",
        },
        "chapters": [
            {"photo_id": "90100401", "title": "卷一 · 银杏与蕨类", "page_count": 10},
            {"photo_id": "90100402", "title": "卷二 · 山茶与苔藓", "page_count": 10},
        ],
    },
    {
        "album_id": "901005",
        "title": "山野来信 · 铁道速写帖",
        "author": "高桥旬",
        "subtitle": "木拱桥、溪谷小站与邮政慢船写生簿",
        "description": "旅途速写画帖：记录高架木桥修缮与山谷邮差日常（演示传输中心失败重试与取消历史）。",
        "motif": "mountain_post",
        "views": 7410,
        "likes": 1180,
        "wishlist": "failed",
        "auto_tags": ["速写", "建筑", "铁道", "旅行"],
        "custom_tags": ["待补完"],
        "palette": {
            "paper": "#F5EFE6",
            "art_bg": "#E9DFD3",
            "primary": "#284D7A",
            "secondary": "#6C7F96",
            "accent": "#B8503C",
            "gleam": "#F0E8DD",
            "ink": "#21211F",
            "seal": "#8F2D22",
        },
        "chapters": [
            {"photo_id": "90100501", "title": "第01话 · 溪谷木桥", "page_count": 18},
            {"photo_id": "90100502", "title": "第02话 · 邮政慢船", "page_count": 16},
        ],
    },
    {
        "album_id": "901006",
        "title": "白麻画室与冬日茶席",
        "author": "佐藤堇",
        "subtitle": "北窗晨光、粗陶茶碗与器物静物集",
        "description": "尚未下载入阁的心愿单藏书：记录冬日白麻画室内的茶席布置、炭火与器物光影。",
        "motif": "tea_atelier",
        "views": 11280,
        "likes": 2060,
        "wishlist": "none",
        "auto_tags": ["日常", "茶道", "画集", "治愈"],
        "custom_tags": ["心愿单"],
        "palette": {
            "paper": "#F6F5F1",
            "art_bg": "#EAE6DD",
            "primary": "#4A5868",
            "secondary": "#8A8479",
            "accent": "#9E3B33",
            "gleam": "#EFECE4",
            "ink": "#21211F",
            "seal": "#7D3028",
        },
        "chapters": [
            {"photo_id": "90100601", "title": "第01话 · 晨光画布", "page_count": 14},
            {"photo_id": "90100602", "title": "第02话 · 炭火煎茶", "page_count": 14},
        ],
    },
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _work_key(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def demo_lock_name(home: Path) -> str:
    return 'Local\\JMComic_Atelier_Demo_' + hashlib.sha256(os.path.normcase(str(home.resolve())).encode()).hexdigest()[:24]


def seed_demo(demo_home: Path, *, keep_existing: bool = True) -> dict:
    """Seed an absolute new leaf or an owned instance; its parent must already exist.

    Pass keep_existing=False only for an explicit reset. Returns home, db_path and
    reused. A caller serving this instance should hold demo_lock_name(home) for
    its lifetime and configure its paths before importing application modules.
    """
    demo_home = _resolve_demo_home(demo_home)
    paths = sys.modules.get('core.path_utils')
    database = sys.modules.get('core.database')
    if ((paths and paths.get_path_config() != paths.get_path_config({'JMCONSOLE_HOME': str(demo_home)}))
            or (database and database.DB_PATH != demo_home / 'runtime/data/app.db')):
        raise RuntimeError("当前进程已绑定其他实例，须在导入 core 前选择演示目录")
    # 必须在导入任何 core.* 模块之前锁定 JMCONSOLE_HOME
    for env_key in ("JMCONSOLE_DOWNLOAD_DIR", "JMCONSOLE_CACHE_DIR", "JMCONSOLE_BACKUP_DIR"):
        os.environ.pop(env_key, None)
    os.environ["JMCONSOLE_HOME"] = str(demo_home)

    if str(ATELIER_ROOT) not in sys.path:
        sys.path.insert(0, str(ATELIER_ROOT))

    if os.name == 'nt':
        from core.instance_lock import maintenance_lock
        with maintenance_lock(demo_lock_name(demo_home)):
            return _seed_owned(demo_home, keep_existing)
    return _seed_owned(demo_home, keep_existing)


def _seed_owned(demo_home: Path, keep_existing: bool) -> dict:
    created = _prepare_home(demo_home)
    db_file = demo_home / "runtime" / "data" / "app.db"
    ready = demo_home / READY_FILE
    if not created and keep_existing:
        if not ready.is_file() or not db_file.is_file() or not db_file.stat().st_size:
            raise ValueError("演示库不完整；复用模式不会清理。如需重建此演示库，请显式指定 --rebuild")
        import core.database as db
        from core.database_schema import CURRENT_SCHEMA_VERSION
        from core.settings import update_settings

        if db._existing_schema() != CURRENT_SCHEMA_VERSION:
            raise ValueError("演示库版本不匹配；复用模式不会重建")
        refresh_conn = db.get_db()
        try:
            with refresh_conn:
                ids = [book['album_id'] for book in DEMO_BOOKS]
                slots = ','.join('?' for _ in ids)
                refresh_conn.execute(
                    f"UPDATE album_detail_cache SET cached_at = ? WHERE album_id IN ({slots})",
                    (datetime.now().isoformat(), *ids),
                )
                refresh_conn.execute("UPDATE OR IGNORE album_tags SET source = 'user' WHERE source = 'custom'")
                refresh_conn.execute("DELETE FROM album_tags WHERE source = 'custom'")
        finally:
            refresh_conn.close()
        update_settings({"auto_update_check": "false"})
        return {"home": str(demo_home), "db_path": str(db_file), "reused": True}

    if not created:
        if any((db_file.parent / name).exists() for name in ('flask.json', 'flask.pid')):
            raise ValueError("演示实例有服务运行记录，拒绝重建")
        roots = [demo_home / name for name in ('runtime', 'downloads') if (demo_home / name).exists()]
        for child in roots:
            if child.resolve() != child or not stat.S_ISDIR(_check_entry(child).st_mode):
                raise ValueError("演示实例目录结构异常；拒绝清理")
        ready.unlink(missing_ok=True)
        for child in roots:
            shutil.rmtree(child)

    from core.path_utils import ensure_dirs, get_cache_dir, get_data_dir, get_download_root
    import core.database as db
    from core.database_schema import CURRENT_SCHEMA_VERSION, validate_schema
    from core.download_layout import CHAPTER_MARKER
    from core.publication import WORK
    from core.settings import update_settings

    ensure_dirs()
    db.init_db()
    update_settings({"ui_theme": "paper", "auto_update_check": "false"})

    download_root = get_download_root()
    covers_dir = get_cache_dir() / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now().replace(microsecond=0)
    tomorrow = now + timedelta(hours=24)

    # 1. 为全部 6 部样书写入本地封面缓存、album_detail_cache、album_meta、album_tags、wishlist
    conn = db.get_db()
    try:
        with conn:
            for idx, book in enumerate(DEMO_BOOKS):
                aid = book["album_id"]
                ts = _iso(now - timedelta(hours=12 - idx))
                cover_bytes = render_cover_bytes(book)
                (covers_dir / f"{aid}.webp").write_bytes(cover_bytes)

                detail_payload = {
                    "album_id": aid,
                    "title": book["title"],
                    "author": book["author"],
                    "tags": book["auto_tags"],
                    "actors": [],
                    "works": ["藏书阁典藏画集"],
                    "views": book["views"],
                    "likes": book["likes"],
                    "description": book["description"],
                    "cover": f"/api/cover/{aid}",
                    "photos": [
                        {
                            "photo_id": ch["photo_id"],
                            "title": ch["title"],
                            "page_count": ch["page_count"],
                            "page_count_known": True,
                        }
                        for ch in book["chapters"]
                    ],
                    "chapter_count": len(book["chapters"]),
                    "details_complete": True,
                }
                conn.execute(
                    "INSERT OR REPLACE INTO album_detail_cache (album_id, detail_json, cover_cdn_url, cached_at) VALUES (?, ?, ?, ?)",
                    (aid, json.dumps(detail_payload, ensure_ascii=False), f"/api/cover/{aid}", datetime.now().isoformat()),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO album_meta
                       (album_id, title, author, cover_url, tags_synced_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (aid, book["title"], book["author"], f"/api/cover/{aid}", ts, ts),
                )
                for tag in book["auto_tags"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO album_tags (album_id, tag, source, created_at) VALUES (?, ?, 'auto', ?)",
                        (aid, tag.lower(), ts),
                    )
                for tag in book["custom_tags"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO album_tags (album_id, tag, source, created_at) VALUES (?, ?, 'user', ?)",
                        (aid, tag.lower(), ts),
                    )
                if book["wishlist"] is not None:
                    conn.execute(
                        """INSERT OR REPLACE INTO wishlist
                           (album_id, title, author, cover_url, added_at, download_status)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (aid, book["title"], book["author"], f"/api/cover/{aid}", ts, book["wishlist"]),
                    )
    finally:
        conn.close()

    def _publish_completed_job(
        *,
        job_id: str,
        book: dict,
        chapters: list[dict],
        created_at: datetime,
        completed_at: datetime,
        as_cbz_only: bool = False,
    ) -> Path:
        """严格按 v5 publication 契约生成不可变任务快照 + 公开书架目录 + 数据库记录。"""
        aid = book["album_id"]
        shelf_name = f"{book['title']}_{aid}"
        shelf_dir = download_root / shelf_name
        work_base = download_root / WORK / _work_key(job_id)
        snapshot_dir = work_base / "snapshot"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (work_base / "workspace.json").write_bytes(
            json.dumps({"job": job_id, "album": aid, "target": shelf_name}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

        manifest = []
        total_pages = sum(ch["page_count"] for ch in chapters)
        chapter_files_map: dict[str, list[tuple[str, bytes]]] = {}

        for ch in chapters:
            folder_name = f"{ch['title']}__{ch['photo_id']}"
            snap_ch_dir = snapshot_dir / folder_name
            snap_ch_dir.mkdir(parents=True, exist_ok=True)
            marker_bytes = json.dumps({"format": 2, "photo_id": ch["photo_id"]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            (snap_ch_dir / CHAPTER_MARKER).write_bytes(marker_bytes)

            files, digests, page_blobs = [], {}, []
            for pno in range(1, ch["page_count"] + 1):
                fname = f"{pno:05d}.webp"
                raw = render_story_page_bytes(book, ch["title"], pno, ch["page_count"])
                (snap_ch_dir / fname).write_bytes(raw)
                files.append(fname)
                digests[fname] = _sha256_bytes(raw)
                page_blobs.append((fname, raw))

            manifest.append(
                {
                    "photo_id": ch["photo_id"],
                    "name": ch["title"],
                    "folder": folder_name,
                    "files": files,
                    "digests": digests,
                }
            )
            chapter_files_map[folder_name] = page_blobs

        (snapshot_dir / ".jm-publication.json").write_bytes(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

        shelf_dir.mkdir(parents=True, exist_ok=True)
        if as_cbz_only:
            cbz_path = shelf_dir / f"{shelf_name}.cbz"
            comic_info = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                "<ComicInfo>\n"
                f"  <Title>{book['title']}</Title>\n"
                f"  <Writer>{book['author']}</Writer>\n"
                f"  <Summary>{book['description']}</Summary>\n"
                "</ComicInfo>\n"
            )
            with zipfile.ZipFile(cbz_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("ComicInfo.xml", comic_info.encode("utf-8"))
                for ch in chapters:
                    folder_name = f"{ch['title']}__{ch['photo_id']}"
                    for fname, raw in chapter_files_map[folder_name]:
                        zf.writestr(f"{folder_name}/{fname}", raw)
        else:
            for ch in chapters:
                folder_name = f"{ch['title']}__{ch['photo_id']}"
                pub_ch_dir = shelf_dir / folder_name
                pub_ch_dir.mkdir(parents=True, exist_ok=True)
                (pub_ch_dir / CHAPTER_MARKER).write_bytes(
                    json.dumps({"format": 2, "photo_id": ch["photo_id"]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                for fname, raw in chapter_files_map[folder_name]:
                    (pub_ch_dir / fname).write_bytes(raw)

        c_iso, f_iso = _iso(created_at), _iso(completed_at)
        selected_ids = json.dumps([ch["photo_id"] for ch in chapters], ensure_ascii=False)
        rel_snapshot = Path(WORK, _work_key(job_id), "snapshot").as_posix()

        with db.transaction() as c:
            cur = c.execute(
                """INSERT INTO jobs
                   (job_id, album_id, title, selected_photo_ids, status, total_pages, done_pages,
                    current_photo, current_image, output_path, error_message,
                    created_at, queued_at, updated_at, completed_at, superseded_at)
                   VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, '', ?, NULL, ?, ?, ?, ?, NULL)""",
                (
                    job_id,
                    aid,
                    book["title"],
                    selected_ids,
                    total_pages,
                    total_pages,
                    chapters[-1]["title"],
                    str(shelf_dir),
                    c_iso,
                    c_iso,
                    f_iso,
                    f_iso,
                ),
            )
            job_order = cur.lastrowid
            generation = _work_key(str(job_order))
            (shelf_dir / ".jm-view.json").write_bytes(
                json.dumps({"job": job_id, "generation": generation}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            c.execute(
                "INSERT INTO published_tasks (job_id, album_id, job_order, snapshot, manifest) VALUES (?, ?, ?, ?, ?)",
                (job_id, aid, job_order, rel_snapshot, json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))),
            )
        return shelf_dir

    books_by_id = {b["album_id"]: b for b in DEMO_BOOKS}

    # 2. 《海岸慢车》(901001)：任务 A（第 1 话 paused 8/20P，含真实工作区断点回执）+ 任务 B（第 2 话 completed 12/12P）
    b1 = books_by_id["901001"]
    paused_job_id = "job_atelier_901001_ch1"
    paused_work_base = download_root / WORK / _work_key(paused_job_id)
    paused_pages_dir = paused_work_base / "pages"
    paused_ch = b1["chapters"][0]
    paused_ch_folder = f"{paused_ch['title']}__{paused_ch['photo_id']}"
    (paused_pages_dir / paused_ch_folder).mkdir(parents=True, exist_ok=True)
    (paused_work_base / "workspace.json").write_bytes(
        json.dumps({"job": paused_job_id, "album": "901001", "target": f"{b1['title']}_901001"}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    (paused_pages_dir / paused_ch_folder / CHAPTER_MARKER).write_bytes(
        json.dumps({"format": 2, "photo_id": paused_ch["photo_id"]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    resume_signatures: dict[str, list[int]] = {}
    receipt_rows: list[tuple[str, str, str]] = []
    for pno in range(1, 9):  # 已写入前 8 / 20 页
        fname = f"{pno:05d}.webp"
        pfile = paused_pages_dir / paused_ch_folder / fname
        raw = render_story_page_bytes(b1, paused_ch["title"], pno, paused_ch["page_count"])
        pfile.write_bytes(raw)
        st = pfile.stat()
        rel_key = f"{paused_ch_folder}/{fname}"
        resume_signatures[rel_key] = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]
        receipt_rows.append((paused_job_id, rel_key, _sha256_bytes(raw)))

    resume_hash = hashlib.sha256(paused_job_id.encode("utf-8")).hexdigest()
    (get_data_dir() / f"resume-{resume_hash}.json").write_bytes(
        json.dumps(resume_signatures, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )

    with db.transaction() as c:
        t_ch1 = _iso(now - timedelta(minutes=42))
        t_ch1_upd = _iso(now - timedelta(minutes=28))
        c.execute(
            """INSERT INTO jobs
               (job_id, album_id, title, selected_photo_ids, status, total_pages, done_pages,
                current_photo, current_image, output_path, error_message,
                created_at, queued_at, updated_at, completed_at, superseded_at)
               VALUES (?, '901001', ?, '["90100101"]', 'paused', 20, 8, ?, '00008.webp', ?, NULL, ?, ?, ?, NULL, NULL)""",
            (paused_job_id, b1["title"], paused_ch["title"], str(paused_pages_dir), t_ch1, t_ch1, t_ch1_upd),
        )
        c.executemany("INSERT INTO task_page_receipts (job_id, page, digest) VALUES (?, ?, ?)", receipt_rows)

    _publish_completed_job(
        job_id="job_atelier_901001_ch2",
        book=b1,
        chapters=[b1["chapters"][1]],
        created_at=now - timedelta(minutes=38),
        completed_at=now - timedelta(minutes=21),
    )

    # 901001 整册归档维护挂起（因第 1 话 paused，JobManager 自动延后执行，前端可见 pending 整册丝带）
    b1_maint_payload = {
        "organize_mode": "none",
        "auto_pack": "true",
        "pack_format": "cbz",
        "delete_originals": "false",
        "directory": "海岸慢车_901001",
        "preferred_directory": "海岸慢车_901001",
        "title_dir": "海岸慢车_901001",
        "author": b1["author"],
        "photos": [
            {
                "photo_id": b1["chapters"][1]["photo_id"],
                "name": b1["chapters"][1]["title"],
                "folder": f"{b1['chapters'][1]['title']}__{b1['chapters'][1]['photo_id']}",
            }
        ],
    }
    with db.transaction() as c:
        c.execute(
            "INSERT OR REPLACE INTO album_maintenance (album_id, state, payload, error, generation) VALUES ('901001', 'pending', ?, '', 1)",
            (json.dumps(b1_maint_payload, ensure_ascii=False, separators=(",", ":")),),
        )

    # 3. 《松林雾山纪行》(901002)：两话全量完成可读
    b2 = books_by_id["901002"]
    _publish_completed_job(
        job_id="job_atelier_901002_all",
        book=b2,
        chapters=b2["chapters"],
        created_at=now - timedelta(hours=5),
        completed_at=now - timedelta(hours=4, minutes=48),
    )

    # 4. 《雨夜钟楼与旧书店》(901003)：纯 CBZ 归档直读标杆
    b3 = books_by_id["901003"]
    _publish_completed_job(
        job_id="job_atelier_901003_cbz",
        book=b3,
        chapters=b3["chapters"],
        created_at=now - timedelta(hours=8),
        completed_at=now - timedelta(hours=7, minutes=50),
        as_cbz_only=True,
    )

    # 5. 《本草植物标本集》(901004)：第 1 卷完成可读 + 整册合卷整理 failed（可点击「重新整理整册」真实离线打包成功）
    b4 = books_by_id["901004"]
    _publish_completed_job(
        job_id="job_atelier_901004_v1",
        book=b4,
        chapters=[b4["chapters"][0]],
        created_at=now - timedelta(hours=3),
        completed_at=now - timedelta(hours=2, minutes=51),
    )
    b4_maint_payload = {
        "organize_mode": "none",
        "auto_pack": "true",
        "pack_format": "cbz",
        "delete_originals": "false",
        "directory": "本草植物标本集_901004",
        "preferred_directory": "本草植物标本集_901004",
        "title_dir": "本草植物标本集_901004",
        "author": b4["author"],
        "photos": [
            {
                "photo_id": b4["chapters"][0]["photo_id"],
                "name": b4["chapters"][0]["title"],
                "folder": f"{b4['chapters'][0]['title']}__{b4['chapters'][0]['photo_id']}",
            }
        ],
        "_attempt": 3,
    }
    with db.transaction() as c:
        c.execute(
            """INSERT OR REPLACE INTO album_maintenance (album_id, state, payload, error, generation)
               VALUES ('901004', 'failed', ?, '自动合卷 CBZ 时目标文件被外部阅读器占用（散图已完整保留，可点击重新整理整册）', 1)""",
            (json.dumps(b4_maint_payload, ensure_ascii=False, separators=(",", ":")),),
        )

    # 6. 《山野来信 · 铁道速写帖》(901005)：先 1 条 canceled、后 1 条 failed（使最新任务状态落入 failed 分组）
    b5 = books_by_id["901005"]
    with db.transaction() as c:
        t_cancel = _iso(now - timedelta(hours=1, minutes=20))
        t_fail = _iso(now - timedelta(minutes=55))
        c.execute(
            """INSERT INTO jobs
               (job_id, album_id, title, selected_photo_ids, status, total_pages, done_pages,
                current_photo, current_image, output_path, error_message,
                created_at, queued_at, updated_at, completed_at, superseded_at)
               VALUES ('job_atelier_901005_ch2', '901005', ?, '["90100502"]', 'canceled', 16, 3,
                       ?, '00004.webp', NULL, '用户手动取消任务',
                       ?, ?, ?, NULL, NULL)""",
            (b5["title"], b5["chapters"][1]["title"], t_cancel, t_cancel, t_cancel),
        )
        c.execute(
            """INSERT INTO jobs
               (job_id, album_id, title, selected_photo_ids, status, total_pages, done_pages,
                current_photo, current_image, output_path, error_message,
                created_at, queued_at, updated_at, completed_at, superseded_at)
               VALUES ('job_atelier_901005_ch1', '901005', ?, '["90100501"]', 'failed', 18, 5,
                       ?, '00006.webp', NULL, '上游节点读取超时（已完成 5/18 页校验，可随时重试续传）',
                       ?, ?, ?, NULL, NULL)""",
            (b5["title"], b5["chapters"][0]["title"], t_fail, t_fail, t_fail),
        )

    # 7. 新章节检查记录（album_update_checks + update_check_log）与检索历史（search_history）
    chk_time = _iso(now - timedelta(minutes=16))
    next_due = _iso(tomorrow)
    with db.transaction() as c:
        # 901001：确认发现第 3 话新章节
        new_901001 = [{"photo_id": "90100103", "index": 3, "title": "第03话 · 潮汐时刻表", "confirmed_at": chk_time}]
        c.execute(
            """INSERT OR REPLACE INTO album_update_checks
               (album_id, baseline_ids, baseline_source, baseline_at, gone_ids, removed_ids,
                upstream_count, new_chapters, new_count, result, last_success_at, last_attempt_at,
                last_trigger, error_kind, error_detail, fail_count, next_check_at, created_at, updated_at)
               VALUES ('901001', '["90100101","90100102"]', 'download', ?, '[]', '[]',
                       3, ?, 1, 'new', ?, ?, 'auto', NULL, NULL, 0, ?, ?, ?)""",
            (_iso(now - timedelta(hours=2)), json.dumps(new_901001, ensure_ascii=False), chk_time, chk_time, next_due, chk_time, chk_time),
        )
        # 901004：确认发现卷二新章节
        new_901004 = [{"photo_id": "90100402", "index": 2, "title": "卷二 · 山茶与苔藓", "confirmed_at": chk_time}]
        c.execute(
            """INSERT OR REPLACE INTO album_update_checks
               (album_id, baseline_ids, baseline_source, baseline_at, gone_ids, removed_ids,
                upstream_count, new_chapters, new_count, result, last_success_at, last_attempt_at,
                last_trigger, error_kind, error_detail, fail_count, next_check_at, created_at, updated_at)
               VALUES ('901004', '["90100401"]', 'download', ?, '[]', '[]',
                       2, ?, 1, 'new', ?, ?, 'manual', NULL, NULL, 0, ?, ?, ?)""",
            (_iso(now - timedelta(hours=3)), json.dumps(new_901004, ensure_ascii=False), chk_time, chk_time, next_due, chk_time, chk_time),
        )
        # 901002 & 901003：检查无更新
        for aid, b_ids, cnt in [
            ("901002", '["90100201","90100202"]', 2),
            ("901003", '["90100301","90100302"]', 2),
        ]:
            c.execute(
                """INSERT OR REPLACE INTO album_update_checks
                   (album_id, baseline_ids, baseline_source, baseline_at, gone_ids, removed_ids,
                    upstream_count, new_chapters, new_count, result, last_success_at, last_attempt_at,
                    last_trigger, error_kind, error_detail, fail_count, next_check_at, created_at, updated_at)
                   VALUES (?, ?, 'download', ?, '[]', '[]', ?, '[]', 0, 'no_update', ?, ?, 'auto', NULL, NULL, 0, ?, ?, ?)""",
                (aid, b_ids, _iso(now - timedelta(hours=6)), cnt, chk_time, chk_time, next_due, chk_time, chk_time),
            )

        log_rows = [
            ("901001", "auto", _iso(now - timedelta(minutes=17)), chk_time, "new", 1, 3, 1),
            ("901004", "manual", _iso(now - timedelta(minutes=16)), chk_time, "new", 1, 2, 1),
            ("901002", "auto", _iso(now - timedelta(minutes=25)), _iso(now - timedelta(minutes=25)), "no_update", 1, 2, 0),
            ("901003", "auto", _iso(now - timedelta(minutes=30)), _iso(now - timedelta(minutes=30)), "no_update", 1, 2, 0),
        ]
        c.executemany(
            """INSERT INTO update_check_log
               (album_id, trigger, started_at, finished_at, outcome, requests, upstream_count, new_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            log_rows,
        )

        for idx, kw in enumerate(["海岸慢车", "水野苍", "本草植物标本", "水墨 纪行", "雨夜钟楼"]):
            c.execute(
                "INSERT INTO search_history (keyword, created_at) VALUES (?, ?)",
                (kw, _iso(now - timedelta(minutes=50 - idx * 7))),
            )

    # 8. 严格校验 Schema v5 契约完整性，并通过 core.logger 写入标准诊断日志（同时覆盖「只看问题」与「全部」视图）
    verify_conn = db.get_db()
    try:
        validate_schema(verify_conn, CURRENT_SCHEMA_VERSION)
    finally:
        verify_conn.close()

    from core.logger import log

    log.info(f"藏书阁 Atelier Edition 演示库初始化完成 schema=v{CURRENT_SCHEMA_VERSION} books=6")
    log.info("已挂载《海岸慢车》第02话不可变快照与第01话断点回执 (08/20P)")
    log.warning("album_id=901004 整册 CBZ 合卷暂缓：目标文件被外部阅读器占用，散图保持可读")
    log.error("job_id=job_atelier_901005_ch1 album_id=901005 上游节点读取超时（已保留 5/18 页校验回执）")
    ready.touch(exist_ok=False)

    return {
        "home": str(demo_home),
        "db_path": str(db_file),
        "downloads_dir": str(download_root),
        "books_seeded": len(DEMO_BOOKS),
        "reused": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    summary = seed_demo(Path(args.home) if args.home else DEFAULT_DEMO_HOME, keep_existing=args.keep)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
