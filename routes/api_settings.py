"""
设置 API
第一版只保存 DEFAULT_SETTINGS 中定义的 key。
download_root 固定为项目 downloads/，不接受修改。
"""
import io
import json
import threading

from flask import Blueprint, request, jsonify, send_file

from core.settings import DEFAULT_SETTINGS, get_settings, normalize_settings_patch, update_settings
from core.logger import log
from core.jm_service import invalidate_option_cache
from core.scheduler import sync_with_settings as sync_scheduler
from core.validation import is_safe_upload_filename

api_settings_bp = Blueprint("api_settings", __name__)


@api_settings_bp.after_request
def _no_store_settings(response):
    response.headers["Cache-Control"] = "no-store"
    return response

_NETWORK_KEYS = {"proxy", "timeout", "retry_times", "client_type", "image_threads", "photo_threads"}
_SCHEDULE_KEYS = {"schedule_enabled", "schedule_start", "schedule_end"}
_apply_lock = threading.Lock()


def _apply_saved_settings(changed: set[str]) -> list[str]:
    """Persistence already committed: report live-application failures honestly."""
    actions = []
    if changed & _NETWORK_KEYS:
        actions.append(("网络客户端", invalidate_option_cache))
    if changed & _SCHEDULE_KEYS:
        actions.append(("定时调度", sync_scheduler))
    if not actions:
        return []
    warnings = []
    with _apply_lock:
        for name, action in actions:
            try:
                action()
            except Exception as exc:
                # Settings can contain credentials; log only the operation and error type.
                log.error(f"设置已保存但应用失败 component={name} error_type={type(exc).__name__}")
                warnings.append(f"{name}未能立即应用，请重启程序后检查")
    return warnings


@api_settings_bp.get("/api/settings")
def list_settings():
    """获取所有设置项"""
    log.debug("API获取设置")
    settings = get_settings()
    response = jsonify({"status": "ok", "settings": settings})
    response.headers["Cache-Control"] = "no-store"
    return response


@api_settings_bp.post("/api/settings")
def save_settings():
    """保存设置项（download_root 为只读，不接受修改）"""
    body = request.get_json(force=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "请求体需为 JSON 对象"}), 400

    # 拒绝修改 download_root
    if "download_root" in body:
        del body["download_root"]

    try:
        changed = update_settings(body)
    except ValueError as exc:
        return jsonify(status="error", message=str(exc)), 400
    warnings = _apply_saved_settings(changed)
    updated = get_settings()
    if (
        (set(body) & _SCHEDULE_KEYS)
        and updated.get("schedule_enabled") == "true"
        and updated.get("schedule_start") == updated.get("schedule_end")
    ):
        warnings.append("定时下载开始与结束整点相同，自动调度窗口将不会开启")
    log.info(f"API保存设置 keys_updated={list(body.keys())}")
    return jsonify({"status": "ok", "saved": True, "settings": updated, "warnings": warnings})


@api_settings_bp.post("/api/settings/reset")
def reset_settings():
    """还原默认配置（scope='appearance' 仅还原主题与动效，默认还原全部可写设置）。"""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "请求体需为 JSON 对象"}), 400
    scope = str(body.get("scope", "all") or "all").strip()
    if scope == "appearance":
        patch = {
            key: DEFAULT_SETTINGS[key]
            for key in ("ui_theme", "ui_fx_enabled", "ui_fx_preset")
        }
        update_settings(patch)
        updated = get_settings()
        log.info("API还原默认外观设置")
        return jsonify({"status": "ok", "saved": True, "scope": scope, "settings": updated, "warnings": []})
    if scope != "all":
        return jsonify({"status": "error", "message": "未知的还原范围"}), 400
    raw_preserve = body.get("preserve_proxy", False)
    if isinstance(raw_preserve, bool):
        preserve_proxy = raw_preserve
    elif isinstance(raw_preserve, str) and raw_preserve.strip().lower() in ("true", "false"):
        preserve_proxy = raw_preserve.strip().lower() == "true"
    else:
        return jsonify({"status": "error", "message": "preserve_proxy 必须为布尔值"}), 400
    patch = {
        k: v for k, v in DEFAULT_SETTINGS.items()
        if k != "download_root" and not (preserve_proxy and k == "proxy")
    }
    changed = update_settings(patch)
    warnings = _apply_saved_settings(changed)
    updated = get_settings()
    log.info(f"API还原默认设置 preserve_proxy={preserve_proxy}")
    return jsonify({"status": "ok", "saved": True, "scope": scope, "settings": updated, "warnings": warnings})


# ── 导入 / 导出 ──


@api_settings_bp.get("/api/settings/export")
def export_settings():
    """导出公共设置（不含 download_root）为 JSON 文件下载"""
    log.info("API导出设置")
    settings = get_settings()
    # 导出文件保留目标机器的代理；手工文件可以显式提供 proxy。
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
    if not file.filename or not is_safe_upload_filename(file.filename):
        return jsonify({"status": "error", "message": "上传文件名不合法"}), 400

    # 限制上传文件大小为 1MB
    MAX_SIZE = 1 * 1024 * 1024
    if request.content_length is not None and request.content_length > MAX_SIZE:
        return jsonify({"status": "error", "message": "上传文件过大，最大允许 1MB"}), 400

    try:
        # 安全读取：限制最大 1MB
        raw_bytes = file.read(MAX_SIZE + 1)
        if len(raw_bytes) > MAX_SIZE:
            return jsonify({"status": "error", "message": "上传文件过大，最大允许 1MB"}), 400
        data = json.loads(raw_bytes.decode("utf-8-sig"))
    except Exception as e:
        log.warning(f"设置导入JSON解析失败: {e}")
        return jsonify({"status": "error", "message": "JSON 解析失败，请检查文件格式"}), 400

    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "设置文件内容需为 JSON 对象"}), 400
    if len(data) > 200:
        return jsonify({"status": "error", "message": "设置文件包含过多字段，最多允许 200 项"}), 400

    skipped = 0
    errors = []

    patch = {}
    for key, value in data.items():
        safe_key = str(key)[:64]
        if key not in DEFAULT_SETTINGS:
            skipped += 1
            if len(errors) < 50:
                errors.append(f"未知设置项 '{safe_key}'，已跳过")
            continue
        if key == "download_root":
            skipped += 1
            if len(errors) < 50:
                errors.append("download_root 为只读项，已跳过")
            continue
        try:
            patch.update(normalize_settings_patch({key: value}))
        except ValueError:
            skipped += 1
            if len(errors) < 50:
                errors.append(f"设置 '{safe_key}' 失败，已跳过")

    changed = update_settings(patch)
    imported = len(patch)
    log.info(f"API导入设置 imported={imported} skipped={skipped} errors={len(errors)}")
    warnings = _apply_saved_settings(changed)
    return jsonify({"status": "ok", "imported": imported, "skipped": skipped, "errors": errors,
                    "warnings": warnings})
