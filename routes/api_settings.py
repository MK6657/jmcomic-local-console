"""
设置 API
第一版只保存 DEFAULT_SETTINGS 中定义的 key。
download_root 固定为项目 downloads/，不接受修改。
"""
import io
import json

from flask import Blueprint, request, jsonify, send_file

from core.settings import DEFAULT_SETTINGS, get_settings, update_settings
from core.logger import log
from core.jm_service import invalidate_option_cache
from core.scheduler import sync_with_settings as sync_scheduler

api_settings_bp = Blueprint("api_settings", __name__)


@api_settings_bp.get("/api/settings")
def list_settings():
    """获取所有设置项"""
    log.debug("API获取设置")
    settings = get_settings()
    return jsonify({"status": "ok", "settings": settings})


@api_settings_bp.post("/api/settings")
def save_settings():
    """保存设置项（download_root 为只读，不接受修改）"""
    body = request.get_json(force=True)
    if not body or not isinstance(body, dict):
        return jsonify({"status": "error", "message": "请求体需为 JSON 对象"}), 400

    # 拒绝修改 download_root
    if "download_root" in body:
        del body["download_root"]

    update_settings(body)
    invalidate_option_cache()  # 设置变更后重建 jmcomic Option
    sync_scheduler()  # 定时下载开关即时生效（无需重启）
    updated = get_settings()
    log.info(f"API保存设置 keys_updated={list(body.keys())}")
    return jsonify({"status": "ok", "settings": updated})


# ── 导入 / 导出 ──


@api_settings_bp.get("/api/settings/export")
def export_settings():
    """导出公共设置（不含 download_root）为 JSON 文件下载"""
    log.info("API导出设置")
    settings = get_settings()
    # 代理可能含认证信息；不导出，也不在导入时覆盖本机代理。
    exportable = {
        k: settings[k] for k in DEFAULT_SETTINGS
        if k not in {"download_root", "proxy"}
    }

    buf = io.BytesIO()
    buf.write(json.dumps(exportable, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.seek(0)

    resp = send_file(
        buf,
        mimetype="application/json",
        as_attachment=True,
        download_name="settings.json",
        max_age=0,  # 不缓存：否则一年强缓存默认值会让第二次导出拿到旧文件
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@api_settings_bp.post("/api/settings/import")
def import_settings():
    """导入 JSON 设置文件"""
    file = request.files.get("file")
    if not file:
        return jsonify({"status": "error", "message": "请上传设置文件"}), 400

    # 限制上传文件大小为 1MB
    MAX_SIZE = 1 * 1024 * 1024
    if request.content_length is not None and request.content_length > MAX_SIZE:
        return jsonify({"status": "error", "message": "上传文件过大，最大允许 1MB"}), 400

    try:
        # 安全读取：限制最大 1MB
        raw_bytes = file.read(MAX_SIZE + 1)
        if len(raw_bytes) > MAX_SIZE:
            return jsonify({"status": "error", "message": "上传文件过大，最大允许 1MB"}), 400
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        log.warning(f"设置导入JSON解析失败: {e}")
        return jsonify({"status": "error", "message": "JSON 解析失败，请检查文件格式"}), 400

    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "设置文件内容需为 JSON 对象"}), 400

    imported = 0
    skipped = 0
    errors = []

    for key, value in data.items():
        if key not in DEFAULT_SETTINGS:
            skipped += 1
            errors.append(f"未知设置项 '{key}'，已跳过")
            continue
        if key == "download_root":
            skipped += 1
            errors.append("download_root 为只读项，已跳过")
            continue
        try:
            update_settings({key: value})  # update_settings 内部会处理 str() 和 bool 归一化
            imported += 1
        except Exception as e:
            skipped += 1
            log.warning(f"设置导入项 '{key}' 失败: {e}")
            errors.append(f"设置 '{key}' 失败，已跳过")

    log.info(f"API导入设置 imported={imported} skipped={skipped} errors={len(errors)}")
    invalidate_option_cache()  # 设置变更后重建 jmcomic Option
    sync_scheduler()  # 定时下载开关即时生效（无需重启）
    return jsonify({"status": "ok", "imported": imported, "skipped": skipped, "errors": errors})
