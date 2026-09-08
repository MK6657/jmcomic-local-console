"""
页面路由 —— 返回 Jinja2 模板，传入必要数据
"""
from flask import Blueprint, render_template, request, abort
from core.database import get_all_jobs
from core.logger import log
from core.settings import get_settings

page_bp = Blueprint("page", __name__)


@page_bp.get("/")
def index():
    """首页：搜索框 + 当前任务 + 最近下载"""
    log.info("页面访问 首页")
    all_jobs = get_all_jobs()
    running_jobs = [j for j in all_jobs if j.get("status") == "running"][:5]
    recent_jobs = [j for j in all_jobs if j.get("status") in ("completed", "failed")][:10]

    # 给 running_jobs 计算百分比
    for job in running_jobs:
        total = job.get("total_pages") or 1
        done = job.get("done_pages") or 0
        job["percentage"] = round(done / total * 100, 1)
        job["current_page"] = done

    # 给 recent_jobs 格式化路径
    for job in recent_jobs:
        job["path"] = job.get("output_path") or ""

    return render_template(
        "index.html",
        title="禁漫下载搜索",
        running_jobs=running_jobs,
        recent_jobs=recent_jobs,
    )


@page_bp.get("/search")
def search():
    """搜索页面"""
    keyword = request.args.get("keyword", "")
    sort = request.args.get("sort", "latest")
    page_size = request.args.get("page_size", 20, type=int)
    log.info(f"页面访问 搜索页 keyword={keyword}")
    return render_template("search.html", title="搜索漫画",
                           keyword=keyword, sort=sort, page_size=page_size)


@page_bp.get("/album/<album_id>")
def album_detail(album_id: str):
    """漫画详情页"""
    log.info(f"页面访问 详情页 album_id={album_id}")
    return render_template("detail.html", title="漫画详情", album_id=album_id)


@page_bp.get("/downloads")
def downloads():
    """下载管理页"""
    log.info("页面访问 下载管理页")
    all_jobs = get_all_jobs()
    # 给每个 job 计算百分比和路径别名
    for job in all_jobs:
        total = job.get("total_pages") or 1
        done = job.get("done_pages") or 0
        job["percentage"] = round(done / total * 100, 1)
        job["current_page"] = done
        job["path"] = job.get("output_path") or ""
        job["error"] = job.get("error_message") or ""

    return render_template("downloads.html", title="下载管理", jobs=all_jobs)


@page_bp.get("/preview/<album_id>")
def preview_page(album_id: str):
    """图片预览页"""
    return render_template("preview.html", title="图片预览", album_id=album_id)


@page_bp.get("/preview/job/<job_id>")
def preview_by_job(job_id: str):
    """从 job_id 跳转到预览页"""
    from core.database import get_job
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
    """资源库页"""
    log.info("页面访问 资源库页")
    return render_template("library.html", title="资源库")


@page_bp.get("/settings")
def settings_page():
    """设置页"""
    log.info("页面访问 设置页")
    return render_template("settings.html", title="设置", settings=get_settings())


@page_bp.get("/favicon.ico")
def favicon_ico():
    """浏览器默认请求 favicon.ico → 重定向到 SVG 版本"""
    from flask import redirect
    return redirect("/static/favicon.svg")
