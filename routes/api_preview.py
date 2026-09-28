"""图片预览/本地阅读器 API v2.0.0"""
import re
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request, send_file

import core.database as db
from core import archive_pages, chapter_inventory
from core.path_guard import is_safe_path, DOWNLOAD_ROOT
from core.database import get_completed_job_by_album_id
from core.file_tree import safe_files
from core.local_availability import MAX_IDS, is_page_image, local_states
from core.logger import log
# P1-8 补充 album_id 校验；图片类型与 MIME 统一取自 core.validation
from core.validation import validate_numeric, is_allowed_image, get_image_mime

api_preview_bp = Blueprint("api_preview", __name__)


@api_preview_bp.post("/api/preview/available")
def preview_available():
    """批量判断哪些漫画本地可读（可离线阅读），不可读的为什么不可读。

    Body: {"album_ids": ["123", ...]}（纯数字，最多 MAX_IDS 个）
    → {"status": "ok",
       "readable": [按请求顺序的可读 id],
       "archives": {id: "cbz" | "zip"}                只剩压缩包、从压缩包读的,
       "local": {id: {"state": ..., "archive": ...}}  完成过下载的各部的本地状态（下载管理按它标每个任务）,
       "unavailable": {id: "deleted" | "archive_corrupt" | "archive_empty"},
       "archive_problems": {id: "archive_corrupt" | "archive_empty"}}  本地只剩打不开的压缩包的（不论下载状态分组）
    unavailable 与收藏 / 资源库的“下载过 · 本地文件不可用”同一分组规则（db.download_state：正在下载、
    最近一次失败的不算），值是具体原因：文件已删除 / 压缩包损坏 / 压缩包无可阅读图片。
    archive_problems 与 /read 的去向同一依据（只看本地文件）：这些漫画点“阅读”会打开本地阅读页说明原因，
    所以各页“阅读”按钮的外观按它决定（utils.js readLink.stateFor），而不是按下载状态分组。
    判定规则统一在 core.local_availability，搜索/详情/下载管理/收藏/资源库共用，结果不会互相矛盾。
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "请求体需为 JSON 对象"}), 400
    album_ids = body.get("album_ids")
    if not isinstance(album_ids, list):
        return jsonify({"status": "error", "message": "缺少 album_ids 列表"}), 400
    if len(album_ids) > MAX_IDS:
        return jsonify({"status": "error", "message": f"album_ids 数量过多，最大 {MAX_IDS}"}), 400
    ids = []
    for aid in album_ids:
        # bool 是 int 的子类，必须单独排除；负数/小数转成字符串后不是纯数字，会被拒绝
        text = str(aid) if isinstance(aid, int) and not isinstance(aid, bool) else aid
        if not validate_numeric(text):
            return jsonify({"status": "error", "message": "album_ids 只能包含纯数字"}), 400
        ids.append(text)
    try:
        states = local_states(ids)
        facts = db.album_job_facts(ids)
    except Exception as e:
        log.error(f"本地可读判断失败 count={len(ids)} error={e}")
        return jsonify({"status": "error", "message": "无法判断本地下载状态"}), 500
    ordered = list(dict.fromkeys(ids))
    readable = [aid for aid in ordered if aid in states and states[aid].readable]
    unavailable = {}
    for aid in ordered:
        state = states.get(aid)
        fact = facts.get(aid, {})
        shown = db.download_state(bool(state and state.readable), fact.get("active_job"), fact.get("latest_job"),
                                  fact.get("has_completed"), fact.get("legacy_status"))
        if shown["status_group"] == "none" and shown["files_missing"]:
            unavailable[aid] = state.problem if state else "deleted"
    return jsonify({
        "status": "ok",
        "readable": readable,
        "archives": {aid: state.archive for aid, state in states.items() if state.state == "archive"},
        "local": {aid: {"state": state.state, "archive": state.archive} for aid, state in states.items()},
        "unavailable": unavailable,
        "archive_problems": {aid: state.problem for aid, state in states.items()
                             if state.state in ("archive_corrupt", "archive_empty")},
    })


@api_preview_bp.get("/api/local-chapters/<album_id>")
def local_chapters(album_id: str):
    """本地章节能否证明“只下载了部分章节”（core.chapter_inventory）：
    → {"status": "ok", "partial": {"downloaded": M, "total": N} 或 null, "reason": ...}。
    只读数据库和磁盘：不联网、不建下载任务；章节列表只用详情页刚取到、没过期的缓存。"""
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    try:
        partial, reason = chapter_inventory.partial_chapters(album_id)
    except Exception as e:
        log.error(f"本地章节清点失败 album_id={album_id} error={e}")
        partial, reason = None, "error"
    return jsonify({"status": "ok", "partial": partial, "reason": reason})


def _find_album_dir(album_id: str) -> tuple[Path | None, str | None]:
    """查找 album_id 对应的本地目录

    优先从 jobs 表获取 output_path；
    若没有匹配任务或目录已被删除，返回 None（用户应通过下载管理页进入预览）。
    最近一次完成的任务已被取代（目录被删除后又被重新下载建出，见 core.local_availability）同样返回 None：
    里面是还没完成（或失败 / 已取消）的那次下载写的残缺内容，与“本地可读”的判断一致。
    """
    job = get_completed_job_by_album_id(album_id)
    if job and job.get("output_path") and not job.get("superseded_at"):
        p = Path(job["output_path"])
        if p.is_dir() and is_safe_path(p):
            return p, job.get("title") or p.name
    return None, None


# 压缩包打不开 / 没有图片时告诉阅读器、单页预览和导出为什么（不假装能离线读，也不说成“文件已删除”）
ARCHIVE_PROBLEMS = {
    "corrupt": ("archive_corrupt", "本地压缩包已损坏，无法打开"),
    "empty": ("archive_empty", "本地压缩包里没有可阅读的图片"),
}


# 压缩包暂时打不开（被别的程序占用等）：不是损坏，503 让阅读页保留“重试”
ARCHIVE_BUSY = "本地压缩包暂时打不开（可能被别的程序占用），请稍后再试"


def _busy():
    response = jsonify({"status": "error", "reason": "archive_busy", "message": ARCHIVE_BUSY})
    response.headers["Cache-Control"] = "no-store"
    return response, 503


def archive_problem(index) -> tuple[str, str]:
    """(原因代码, 给用户看的说明)：说明带上压缩包的文件名"""
    reason, text = ARCHIVE_PROBLEMS[index.status]
    return reason, f"{text}（{index.path.name}）。可以重新下载，或者在线阅读。"


# 章节目录名“<章节名>__<photo_id>”（同名时再加“_2”…，见 core.jm_service._chapter_output_dir）
_CHAPTER_ID = re.compile(r"__(\d+)(?:_\d+)?$")


def _page_list(album_id: str, album_dir: Path, loose: list, index) -> list[dict]:
    """散图 + 压缩包补齐的页（archive_pages.merge_pages）→ {page, url, chapter, photo_id, offset}。
    压缩包的页地址是它在阅读器页里的序号，带压缩包版本 v 和页名 p：压缩包换了（重新打包）时服务端按页名找回同一页，
    找不到就说明压缩包已更新（409），不会给出别的页。photo_id / offset（这一页在本章里的位置，从 0 起）
    让“在线阅读这一页”打开同一章的同一页；章节目录名里没有 photo_id 时为 None。"""
    numbers = {}
    if index is not None and index.status == "ok":
        numbers = {page.name: number for number, page in enumerate(index.reader_pages, start=1)}
        version = archive_pages.version(index)
    pages, counts = [], {}
    for name, source in archive_pages.merge_pages(loose, index):
        if isinstance(source, archive_pages.ArchivePage):
            url = (f"/api/preview-archive/{album_id}/{numbers[source.name]}?v={version}"
                   f"&p={quote(source.name, safe='')}")
            chapter = source.chapter.rsplit("/", 1)[-1] if source.chapter else album_dir.name
        else:
            url = "/api/preview-img/" + quote(source.relative_to(DOWNLOAD_ROOT).as_posix(), safe="/")
            chapter = source.parent.name
        folder = name.rsplit("/", 1)[0] if "/" in name else ""
        offset = counts.get(folder, 0)
        counts[folder] = offset + 1
        match = _CHAPTER_ID.search(folder.rsplit("/", 1)[-1]) if folder else None
        pages.append({"page": len(pages) + 1, "url": url, "chapter": chapter,
                      "photo_id": match.group(1) if match else None, "offset": offset})
    return pages


def _scan_pages(album_dir: Path) -> list[tuple[str, Path]]:
    """目录里阅读器能显示的散图 [(相对路径, 文件)]，自然排序（含根目录与各章节子目录）"""
    return [(img.relative_to(album_dir).as_posix(), img) for img in safe_files(album_dir, nonempty=True)
            if is_page_image(img)]  # 与“本地可读”判定（core.local_availability）同一条规则：空文件不算页


@api_preview_bp.get("/api/preview/<album_id>")
def preview_album(album_id: str):
    """获取某个漫画的本地章节和图片列表

    扫描 DOWNLOAD_ROOT/<漫画名>/ 目录下的所有图片，按章节子目录分组，返回扁平化的 pages 列表。
    目录里有压缩包（core.archive_pages：CBZ / 本程序打包的 ZIP）时用它补齐散图没有的页，页地址走 /api/preview-archive；
    同一页散图优先，不会重复。只剩压缩包而它损坏 / 没有图片 → 422，reason 与 message 说明原因。
    source：files 全是散图 / archive 全从压缩包 / mixed 两者都有；archive：用到的压缩包格式。
    """
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    log.info(f"API预览 album_id={album_id}")
    try:
        album_dir, title = _find_album_dir(album_id)
        if not album_dir:
            log.warning(f"预览未找到本地目录 album_id={album_id}")
            return jsonify({"status": "error", "message": "未找到已下载的漫画"}), 404

        loose = _scan_pages(album_dir)
        index = archive_pages.select(album_dir)
        if index is not None and index.transient:
            log.warning(f"预览压缩包暂时打不开 album_id={album_id} archive={index.path.name} detail={index.reason}")
            return _busy()
        if not loose:
            if index is None:
                log.warning(f"预览目录无图片 album_dir={album_dir}")
                return jsonify({"status": "error", "message": "本地没有找到图片"}), 404
            if index.status != "ok":
                reason, message = archive_problem(index)
                log.warning(f"预览压缩包不可用 album_id={album_id} archive={index.path.name} status={index.status} "
                            f"detail={index.reason}")
                return jsonify({"status": "error", "reason": reason, "message": message, "archive": index.format}), 422
        pages = _page_list(album_id, album_dir, loose, index)
        from_archive = sum(1 for page in pages if page["url"].startswith("/api/preview-archive/"))
        source = "files" if not from_archive else ("archive" if from_archive == len(pages) else "mixed")
        log.info(f"API预览成功 album_id={album_id} total_pages={len(pages)} source={source}")
        return jsonify({
            "status": "ok",
            "album_id": album_id,
            "title": title or album_dir.name,
            "total_pages": len(pages),
            "pages": pages,
            "source": source,
            "archive": index.format if from_archive else None,
        })

    except Exception as e:
        log.error(f"API预览失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "预览失败，请检查本地文件是否存在"}), 500


def _packed_away(full_path: Path) -> bool:
    """不在了的散图是否已在它所在漫画目录的压缩包里（按页名 / page_key 找；往上最多看 3 层目录）"""
    try:
        root = DOWNLOAD_ROOT.resolve()
        for folder in list(full_path.parents)[:3]:
            if not folder.is_dir() or not is_safe_path(folder) or folder.resolve() == root:
                continue
            if not archive_pages.candidates(folder):
                continue
            index = archive_pages.select(folder)
            name = full_path.relative_to(folder).as_posix()
            return bool(index is not None and index.status == "ok" and index.find(name))
    except (OSError, ValueError):
        pass
    return False


@api_preview_bp.get("/api/preview-img/<path:img_path>")
def preview_image(img_path: str):
    """返回本地图片文件（经 path_guard 校验）"""
    full_path = DOWNLOAD_ROOT / img_path

    # 路径安全检查 —— 拒绝路径穿越
    if not is_safe_path(full_path):
        log.warning(f"图片访问路径越权 img_path={img_path}")
        return jsonify({"status": "error", "message": "没有权限访问该文件"}), 403

    # 检查文件是否存在
    if not full_path.is_file():
        if _packed_away(full_path):
            # 阅读页打开后这一页被自动打包进了压缩包（删了原图）：让阅读页提示刷新，而不是重试不了的“加载失败”
            response = jsonify({"status": "error", "reason": "archive_changed",
                                "message": "这一页已打包进本地压缩包，请刷新页面后再读"})
            response.headers["Cache-Control"] = "no-store"
            return response, 409
        log.warning(f"图片文件不存在 img_path={img_path}")
        return jsonify({"status": "error", "message": "文件不存在"}), 404

    # 只允许访问 webp/jpg/jpeg/png/gif
    if not is_allowed_image(full_path.suffix):
        log.warning(f"不支持的图片类型 img_path={img_path} suffix={full_path.suffix}")
        return jsonify({"status": "error", "message": "不支持的图片类型"}), 403

    # max_age=0：预览图内容可能因重试/重新下载而变化（URL 不变），
    # 不能吃 SEND_FILE_MAX_AGE_DEFAULT 的一年强缓存；保留 ETag 走 304 协商缓存
    return send_file(str(full_path), mimetype=get_image_mime(full_path.suffix), max_age=0)


@api_preview_bp.get("/api/preview-archive/<album_id>/<int:number>")
def preview_archive_image(album_id: str, number: int):
    """压缩包里的第 number 页（与 /api/preview 列出的顺序相同）：只在内存里解出这一页，不写进下载目录。
    压缩包由服务端按这部漫画最近一次完成的下载找（与 /api/preview 相同），不接受客户端给的路径。
    带 v（压缩包版本）且压缩包已经换了（重新打包）时按 p（页名）找回同一页；找不到 → 409 archive_changed，
    阅读页据此提示刷新，而不是悄悄显示别的页。"""
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    album_dir, _ = _find_album_dir(album_id)
    index = archive_pages.select(album_dir) if album_dir else None
    if index is None:
        return jsonify({"status": "error", "message": "未找到本地压缩包"}), 404
    if index.transient:
        return _busy()
    if index.status != "ok":
        reason, message = archive_problem(index)
        return jsonify({"status": "error", "reason": reason, "message": message}), 422
    pages = index.reader_pages
    wanted = request.args.get("v")
    if wanted is not None and wanted != archive_pages.version(index):
        found = index.find(request.args.get("p") or "")
        if found is None:
            response = jsonify({"status": "error", "reason": "archive_changed",
                                "message": "本地压缩包已更新，请刷新页面后再读"})
            response.headers["Cache-Control"] = "no-store"
            return response, 409
        number, page = found
    elif not 1 <= number <= len(pages):
        return jsonify({"status": "error", "message": "页码超出范围"}), 404
    else:
        page = pages[number - 1]
    try:
        data = archive_pages.read_page(index, page)
    except archive_pages.ArchiveBusy as e:
        log.warning(f"压缩包页暂时读不了 album_id={album_id} archive={index.path.name} page={page.name} error={e}")
        return _busy()
    except archive_pages.ArchiveError as e:
        log.warning(f"压缩包页读取失败 album_id={album_id} archive={index.path.name} page={page.name} error={e}")
        return jsonify({"status": "error", "reason": "archive_corrupt",
                        "message": "压缩包里这一页读不出来，压缩包可能已损坏"}), 422
    response = Response(data, mimetype=get_image_mime(page.suffix))
    # 与散图一样不做强缓存（地址带压缩包版本）；ETag 让重复请求走 304
    response.headers["Cache-Control"] = "no-cache"
    response.set_etag(f"{archive_pages.version(index)}-{number}")
    return response.make_conditional(request)
