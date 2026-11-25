"""
页面路由 —— 返回 Jinja2 模板，传入必要数据
"""
from contextlib import closing

from flask import Blueprint, request, abort, redirect, current_app
from core.interface import render_page as render_template
from core import download_folios, local_availability
from core.database import get_db, get_job
from core.logger import log
from core.settings import get_settings
from core.validation import validate_numeric, validate_job_id

page_bp = Blueprint("page", __name__)


def _safe_local_next(raw_next: str) -> str:
    candidate = (raw_next or '').strip()
    if (
        candidate.startswith('/')
        and not candidate.startswith('//')
        and '\\' not in candidate
        and '\r' not in candidate
        and '\n' not in candidate
        and not candidate.startswith('/ui/')
    ):
        return candidate
    return '/downloads'


@page_bp.get('/ui/<edition>')
def select_edition(edition):
    if edition not in ('classic', 'atelier'):
        abort(404)
    target = _safe_local_next(request.args.get('next', ''))
    response = redirect(target, code=303)
    response.set_cookie('jmconsole_edition', edition, max_age=365 * 86400, httponly=True, samesite='Strict')
    response.headers['Cache-Control'] = 'no-store'
    return response


@page_bp.get("/")
def index():
    """首页：搜索框 + 当前任务 + 最近下载"""
    log.info("页面访问 首页")
    show_recent = get_settings().get("home_show_recent_downloads") == "true"
    with closing(get_db()) as conn:
        running_jobs = [dict(row) for row in conn.execute(
            "SELECT * FROM jobs WHERE status = 'running' ORDER BY created_at DESC, id DESC LIMIT 5")]
        recent_jobs = [dict(row) for row in conn.execute(
            "SELECT * FROM jobs WHERE status IN ('completed', 'failed') ORDER BY created_at DESC, id DESC LIMIT 10"
        ).fetchall()] if show_recent else []

    download_folios.resolve_job_titles(running_jobs + recent_jobs)

    from core import path_guard
    from core.publication import private_path
    for job in recent_jobs:
        raw_out = job.get("output_path") or ""
        if job.get("status") != "completed" or (raw_out and private_path(raw_out, path_guard.DOWNLOAD_ROOT)):
            job["output_path"] = ""

    # 给 running_jobs 计算百分比
    for job in running_jobs:
        total = job.get("total_pages") or 0
        done = job.get("done_pages") or 0
        job["percentage"] = min(100.0, max(0.0, round(done / total * 100, 1))) if total > 0 else 0.0
        job["current_page"] = done

    return render_template(
        "index.html",
        title="禁漫下载搜索",
        running_jobs=running_jobs,
        recent_jobs=recent_jobs,
        show_recent_downloads=show_recent,
    )


@page_bp.get("/search")
def search():
    """搜索页面"""
    keyword = request.args.get("keyword", "")
    sort = request.args.get("sort", "latest")
    page_size = request.args.get("page_size", 20, type=int)
    log.info(f"页面访问 搜索页 keyword={keyword}")
    return render_template("search.html", title="搜索漫画",
                           keyword=keyword, sort=sort, page_size=page_size,
                           search_history_session=current_app.config["SEARCH_HISTORY_SESSION"])


@page_bp.get("/album/<album_id>")
def album_detail(album_id: str):
    """漫画详情页"""
    if not validate_numeric(album_id):
        abort(400)
    log.info(f"页面访问 详情页 album_id={album_id}")
    return render_template("detail.html", title="漫画详情", album_id=album_id)


@page_bp.get("/downloads")
def downloads():
    """下载管理页"""
    log.info("页面访问 下载管理页")
    return render_template("downloads.html", title="下载管理")


@page_bp.get("/preview/<album_id>")
def preview_page(album_id: str):
    """图片预览页（单页翻页）；与连续阅读一样只接受纯数字 album_id"""
    if not validate_numeric(album_id):
        abort(400)
    return render_template("preview.html", title="图片预览", album_id=album_id)


def _archive_problem(album_id: str) -> bool:
    """本地只剩打不开的压缩包（损坏 / 没有可阅读的图片）"""
    state = local_availability.local_state(album_id)
    return bool(state and state.problem in ("archive_corrupt", "archive_empty"))


@page_bp.get("/read/<album_id>")
def continuous_reader(album_id: str):
    """“阅读”入口，各页面的“阅读”按钮都指向这里，点击时才决定去向：
    本地可读（core.local_availability，全站同一标准：散图，或只剩能读的压缩包）→ 连续阅读本地文件；
    本地只有损坏 / 没有图片的压缩包 → 仍打开本地阅读页，由它说明压缩包的问题（不悄悄转去在线阅读）；
    否则 → 在线阅读 /online/<id>（保留 ?page=）。
    页面打开后才下载完成、或本地文件被删除，点“阅读”也能去对地方。从不启动下载。"""
    if not validate_numeric(album_id):
        abort(400)
    if not local_availability.is_readable(album_id) and not _archive_problem(album_id):
        page = request.args.get("page", type=int)
        log.info(f"本地未下载，转到在线阅读 album_id={album_id}")
        return redirect(f"/online/{album_id}" + (f"?page={page}" if page and page > 0 else ""))
    return render_template("reader.html", title="连续阅读", album_id=album_id, online=False)


@page_bp.get("/online/<album_id>")
def online_reader_page(album_id: str):
    """在线阅读：同一连续阅读页，图片经本程序按需从上游获取；不下载、不写入资源库"""
    if not validate_numeric(album_id):
        abort(400)
    return render_template("reader.html", title="在线阅读", album_id=album_id, online=True)


@page_bp.get("/preview/job/<job_id>")
def preview_by_job(job_id: str):
    """从 job_id 跳转到预览页"""
    if not validate_job_id(job_id):
        abort(400)
    job = get_job(job_id)
    if not job:
        log.warning(f"预览页: 不存在的 job_id={job_id}")
        abort(404)
    return render_template("preview.html", title="图片预览", album_id=job["album_id"])


@page_bp.get("/wishlist")
def wishlist_page():
    """收藏清单页"""
    log.info("页面访问 收藏清单页")
    return render_template("wishlist.html", title="收藏清单")


@page_bp.get("/library")
def library_page():
    """资源库页；card_click：卡片正文空白处点一下是否打开详情（设置 library_card_click，默认关）"""
    log.info("页面访问 资源库页")
    card_click = get_settings().get("library_card_click") == "true"
    return render_template("library.html", title="资源库", card_click=card_click)


@page_bp.get("/settings")
def settings_page():
    """设置页"""
    log.info("页面访问 设置页")
    return render_template("settings.html", title="设置", settings=get_settings())


@page_bp.get("/favicon.ico")
def favicon_ico():
    """浏览器默认请求 favicon.ico → 重定向到 SVG 版本"""
    return redirect("/static/favicon.svg")
