"""
System API 路由
"""
import time
import os
import socket
from datetime import datetime
from pathlib import Path
from flask import Blueprint, jsonify, request
from core.database import get_db, get_all_jobs
from core.logger import log, LOG_DIR, MAX_TOTAL_SIZE_MB, MAX_LOG_AGE_DAYS
from core.validation import validate_numeric
from core.jm_service import get_active_client_count, invalidate_option_cache
from core.settings import build_jmcomic_option
from urllib.parse import urlparse
import re as _re

api_system_bp = Blueprint("api_system", __name__)

# 进程启动时间（用于计算 uptime）
_START_TIME = time.time()


@api_system_bp.get("/api/system/diagnose")
def diagnose():
    """系统自检端点，返回健康状态和诊断信息"""
    try:
        now = time.time()
        uptime_seconds = int(now - _START_TIME)
        issues = []
        level = "healthy"

        # ── 日志系统状态 ──
        log_files = sorted(
            [f for f in LOG_DIR.iterdir() if f.is_file() and f.name.startswith(('app', 'error'))],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        total_log_size = 0
        main_files = []
        error_files = []
        for f in log_files:
            sz = f.stat().st_size
            total_log_size += sz
            if f.name.startswith('app'):
                main_files.append({"name": f.name, "size_kb": round(sz / 1024, 1)})
            elif f.name.startswith('error'):
                error_files.append({"name": f.name, "size_kb": round(sz / 1024, 1)})

        log_status = {
            "main_log": {"files": main_files[:10], "count": len(main_files)},
            "error_log": {"files": error_files[:10], "count": len(error_files)},
            "total_size_mb": round(total_log_size / 1024 / 1024, 2),
            "max_size_mb": MAX_TOTAL_SIZE_MB,
            "max_age_days": MAX_LOG_AGE_DAYS,
        }
        if total_log_size > MAX_TOTAL_SIZE_MB * 1024 * 1024 * 0.9:
            issues.append(f"日志总大小 {log_status['total_size_mb']:.1f}MB，接近上限 {MAX_TOTAL_SIZE_MB}MB")
            level = "degraded"

        # ── 应用状态 ──
        db_ok = False
        try:
            conn = get_db()
            conn.execute("SELECT 1").fetchone()
            conn.close()
            db_ok = True
        except Exception:
            issues.append("数据库连接异常")
            level = "unhealthy"

        # ── 错误日志最近 24h 分析 ──
        _error_pattern = _re.compile(r'\b(ERROR|CRITICAL)\b')
        errors_24h = 0
        recent_errors = []
        cutoff = datetime.now().timestamp() - 86400
        _MAX_SCAN_SIZE = 10 * 1024 * 1024
        for f in log_files:
            if not f.name.startswith('error'):
                continue
            if f.stat().st_mtime < cutoff:
                continue
            if f.stat().st_size > _MAX_SCAN_SIZE:
                continue
            try:
                with open(f, 'r', encoding='utf-8', errors='replace') as fh:
                    for line in fh:
                        if fh.tell() > _MAX_SCAN_SIZE:
                            break
                        if _error_pattern.search(line):
                            errors_24h += 1
                            if len(recent_errors) < 5:
                                recent_errors.append(line.strip()[:200])
            except Exception:
                pass

        if errors_24h > 20:
            issues.append(f"最近 24 小时错误 {errors_24h} 次（阈值 20）")
            if level == "healthy":
                level = "degraded"

        active_count = 0
        queued_count = 0
        try:
            all_jobs = get_all_jobs()
            active_count = sum(1 for j in all_jobs if j.get("status") == "running")
            queued_count = sum(1 for j in all_jobs if j.get("status") == "queued")
        except Exception:
            pass

        # ── jmcomic API 域名可达性检查 ──
        jm_domain = ""
        jm_reachable = False
        try:
            opt = build_jmcomic_option()
            domain_list = opt.get("client", {}).get("domain", [])
            if domain_list:
                jm_domain = domain_list[0]
            else:
                from jmcomic import JmModuleConfig
                defaults = JmModuleConfig.default_dict()
                domain_list = defaults.get("client", {}).get("domain", [])
                jm_domain = domain_list[0] if domain_list else ""
            if jm_domain:
                parsed = urlparse(jm_domain)
                host = parsed.hostname or jm_domain
                port = parsed.port or 443
                sock = socket.create_connection((host, port), timeout=3)
                sock.close()
                jm_reachable = True
        except Exception:
            jm_reachable = False
            if jm_domain:
                issues.append(f"jmcomic API 域名 {jm_domain} 不可达")
                if level == "healthy":
                    level = "degraded"

        return jsonify({
            "status": "ok",
            "health": level,
            "uptime": {
                "seconds": uptime_seconds,
                "since": datetime.fromtimestamp(_START_TIME).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "log_system": log_status,
            "application": {
                "db_ok": db_ok,
                "active_jobs": active_count,
                "queued_jobs": queued_count,
            },
            "jmcomic": {
                "domain": jm_domain,
                "reachable": jm_reachable,
                "active_clients": get_active_client_count(),
            },
            "warnings": issues,
            "error_count_24h": errors_24h,
        })
    except Exception as e:
        log.exception(f"诊断过程异常 error={e}")
        return jsonify({"status": "error", "message": "诊断过程出错", "health": "unhealthy"}), 500


@api_system_bp.get("/api/system/health")
def health_ping():
    """轻量健康检查"""
    log.debug("API健康检查")
    db_ok = False
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ok = True
    except Exception:
        pass
    if not db_ok:
        return jsonify({"status": "error", "message": "数据库连接不可用", "db": "unreachable", "ts": datetime.now().isoformat()}), 503
    return jsonify({
        "status": "ok",
        "ts": datetime.now().isoformat(),
        "application": "jmcomic-local-console",
        "pid": os.getpid(),
        "jm_active_clients": get_active_client_count(),
    })


@api_system_bp.post("/api/system/clear-cache")
def clear_jm_comic_cache():
    """清除 jmcomic 客户端缓存（Option + Client），强制下一次请求重建。"""
    log.info("API清除 jmcomic 缓存")
    try:
        invalidate_option_cache()
        return jsonify({"status": "ok", "message": "jmcomic 缓存已清除"})
    except Exception as e:
        log.error(f"清除 jmcomic 缓存失败: {e}")
        return jsonify({"status": "error", "message": "清除缓存失败"}), 500
