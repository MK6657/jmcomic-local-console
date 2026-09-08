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
    """批量更新设置，只保存 DEFAULT_SETTINGS 中存在的 key，更新后清空缓存"""
    # 枚举白名单
    # 枚举白名单
    _VALID_CLIENT_TYPES = {"api", "html"}
    _VALID_PACK_FORMATS = {"cbz", "pdf", "zip"}
    _VALID_ORGANIZE_MODES = {"none", "by_author", "flat"}  # 与 jm_service.organize_download 一致
    _VALID_BOOL_KEYS = {"schedule_enabled", "auto_pack", "delete_originals", "skip_existing"}

    for key, value in settings.items():
        if key in DEFAULT_SETTINGS:
            # download_root 固定为项目目录
            if key == "download_root":
                value = str(DOWNLOAD_ROOT)
            # 确保布尔值存储为小写字符串 'true'/'false'
            if isinstance(value, bool):
                value = "true" if value else "false"
            # 枚举白名单校验
            if key == "client_type" and str(value).strip() not in _VALID_CLIENT_TYPES:
                log.warning(f"设置项 'client_type' 值 '{value}' 无效，已跳过")
                continue
            if key == "pack_format" and str(value).strip() not in _VALID_PACK_FORMATS:
                log.warning(f"设置项 'pack_format' 值 '{value}' 无效，已跳过")
                continue
            if key == "organize_mode" and str(value).strip() not in _VALID_ORGANIZE_MODES:
                log.warning(f"设置项 'organize_mode' 值 '{value}' 无效，已跳过")
                continue
            # 布尔字符串校验
            if key in _VALID_BOOL_KEYS:
                str_val = str(value).strip().lower()
                if str_val not in ("true", "false"):
                    log.warning(f"设置项 '{key}' 值 '{value}' 不是有效布尔值，已跳过")
                    continue
                value = str_val
            # Proxy URL 格式校验（非空时）
            if key == "proxy" and value:
                parsed = urlparse(str(value))
                if not parsed.scheme or not parsed.netloc:
                    log.warning("设置项 'proxy' 不是有效 URL，已跳过（地址已隐藏）")
                    continue
            # 类型/范围校验
            if key in ("timeout", "retry_times", "max_running_jobs", "image_threads", "photo_threads"):
                try:
                    int_val = int(str(value))
                    if int_val < 1:
                        raise ValueError(f"{key} 必须 ≥ 1")
                    if key in ("image_threads", "photo_threads") and int_val > 50:
                        raise ValueError(f"{key} 最大为 50")
                    if key == "retry_times" and int_val > 10:
                        raise ValueError("retry_times 最大为 10")
                    if key == "max_running_jobs" and int_val > 5:
                        raise ValueError("max_running_jobs 最大为 5")
                except (ValueError, TypeError) as e:
                    log.warning(f"设置项 '{key}' 值校验失败: {e}，已跳过")
                    continue
            if key in ("schedule_start", "schedule_end"):
                try:
                    hour = int(str(value))
                    if hour < 0 or hour > 23:
                        log.warning(f"设置项 '{key}' 值 {value} 不在 0-23 范围内，已跳过")
                        continue
                except (ValueError, TypeError):
                    log.warning(f"设置项 '{key}' 值 {value} 不是有效小时，已跳过")
                    continue
            db.set_setting(key, str(value))

    # 写入后清空缓存，确保下次读取最新数据
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
