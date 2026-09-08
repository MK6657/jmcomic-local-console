"""
设置管理模块
第一版只保留真实生效的设置项。
"""
import threading
import time
from urllib.parse import urlparse

from . import database as db
from .logger import log
from .path_guard import DOWNLOAD_ROOT

# ── 设置缓存（5 秒 TTL，避免密集调 get_settings 时反复查 DB）──
_settings_cache: dict | None = None
_settings_cache_time: float = 0
_SETTINGS_CACHE_TTL = 5.0
_settings_lock = threading.Lock()

# 第一版支持的设置项及默认值
DEFAULT_SETTINGS = {
    "download_root": str(DOWNLOAD_ROOT),
    "max_running_jobs": "1",
    "timeout": "30",
    "retry_times": "2",
    "proxy": "",
    "client_type": "api",
    "skip_existing": "true",
    "organize_mode": "none",
    # 定时下载设置
    "schedule_enabled": "false",
    "schedule_start": "23",
    "schedule_end": "7",
    # 下载后自动打包设置
    "auto_pack": "false",
    "pack_format": "cbz",
    "delete_originals": "false",
    # 图片/章节并发数设置
    "image_threads": "20",
    "photo_threads": "1",
}


def get_settings() -> dict:
    """获取所有设置（合并默认值），5 秒缓存"""
    global _settings_cache, _settings_cache_time
    now = time.time()
    with _settings_lock:
        if _settings_cache is not None and (now - _settings_cache_time) < _SETTINGS_CACHE_TTL:
            return dict(_settings_cache)
        stored = db.get_all_settings()
        merged = dict(DEFAULT_SETTINGS)
        merged.update(stored)
        _settings_cache = merged
        _settings_cache_time = now
        return dict(merged)


def invalidate_settings_cache():
    """使设置缓存失效（保存设置后调用）"""
    global _settings_cache
    with _settings_lock:
        _settings_cache = None


def get_setting(key: str) -> str:
    """获取单个设置"""
    return db.get_setting(key, DEFAULT_SETTINGS.get(key, ""))


def update_settings(settings: dict):
    """Validate the whole patch, then commit it atomically; never report a silent skip."""
    enums = {
        "client_type": {"api", "html"}, "pack_format": {"cbz", "zip"},
        "organize_mode": {"none", "by_author", "flat"},
    }
    bounds = {
        "timeout": (5, 120), "retry_times": (0, 20), "max_running_jobs": (1, 5),
        "image_threads": (1, 50), "photo_threads": (1, 10),
        "schedule_start": (0, 23), "schedule_end": (0, 23),
    }
    bool_keys = {"schedule_enabled", "auto_pack", "delete_originals", "skip_existing"}
    normalized = {}
    for key, value in settings.items():
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"未知设置项: {key}")
        if key == "download_root":
            normalized[key] = str(DOWNLOAD_ROOT)
            continue
        if key in bool_keys:
            if isinstance(value, bool):
                value = "true" if value else "false"
            if not isinstance(value, str) or value.strip().lower() not in {"true", "false"}:
                raise ValueError(f"{key} 必须为 true 或 false")
            normalized[key] = value.strip().lower()
        elif key in bounds:
            lo, hi = bounds[key]
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError(f"{key} 必须是 {lo}–{hi} 的整数")
            try:
                number = int(value)
            except (ValueError, TypeError):
                raise ValueError(f"{key} 必须是 {lo}–{hi} 的整数") from None
            if not lo <= number <= hi:
                raise ValueError(f"{key} 必须是 {lo}–{hi} 的整数")
            normalized[key] = str(number)
        elif key in enums:
            if not isinstance(value, str) or value.strip() not in enums[key]:
                raise ValueError(f"{key} 的选项无效")
            normalized[key] = value.strip()
        elif key == "proxy":
            if not isinstance(value, str):
                raise ValueError("proxy 必须是字符串")
            value = value.strip()
            try:
                if value:
                    parsed = urlparse(value)
                    if (parsed.scheme not in {"http", "https", "socks5", "socks5h"}
                            or not parsed.hostname or parsed.port == 0
                            or any(char.isspace() for char in value)):
                        raise ValueError()
            except ValueError:
                raise ValueError("proxy 地址无效（认证信息已隐藏）") from None
            normalized[key] = value
    conn = db.get_db()
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                list(normalized.items()),
            )
    finally:
        conn.close()
    invalidate_settings_cache()


def build_jmcomic_option() -> dict:
    """根据当前设置生成 jmcomic option 配置字典"""
    s = get_settings()

    option = {
        "download": {
            "image": {
                "suffix": "original",  # 第一版固定webp
            },
            "threading": {
                "image": int(s.get("image_threads", 20)),
                "photo": int(s.get("photo_threads", 1)),
            },
        },
        "client": {
            "impl": "html",
            "retry_times": int(s.get("retry_times", 2)),  # 降为 2，网络稳定时减少重试等待
            "timeout": int(s.get("timeout", 30)),
            # ⭐ 当前可用的 API 域名（jmcomic 内部默认域名可能已过期）
            "domain": [
                "https://www.cdnhth.club",
                "https://www.cdnmhwscc.vip",
                "https://www.jmapiproxyxxx.vip",
                "https://www.cdnxxx-proxy.xyz",
                "https://www.jmeadpoolcdn.life",
            ],
            # ⭐ 启用 session 模式，复用 TCP 连接，消除每次请求的 TLS 握手
            "postman": {
                "type": "curl_cffi_session",
                "meta_data": {
                    "impersonate": "chrome",
                    # ⭐ 显式设置 HTTP 请求超时（秒），防止 curl_easy_perform 无限阻塞
                    "timeout": int(s.get("timeout", 30)),
                },
            },
        },
        "dir_rule": {
            "base_dir": str(DOWNLOAD_ROOT),
        },
    }

    client_type = s.get("client_type", "html")
    if client_type == "api":
        option["client"]["impl"] = "api"

    proxy = s.get("proxy", "").strip()
    if proxy:
        option["client"]["postman"]["meta_data"]["proxies"] = {
            "http": proxy, "https": proxy,
        }

    return option
