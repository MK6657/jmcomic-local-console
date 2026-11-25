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
from .ui_themes import (
    DEFAULT_UI_FX_ENABLED,
    DEFAULT_UI_FX_PRESET,
    DEFAULT_UI_THEME,
    UI_FX_PRESETS,
    UI_THEMES,
    normalize_fx_preset,
    normalize_theme,
)

# ── 设置缓存（5 秒 TTL，避免密集调 get_settings 时反复查 DB）──
_settings_cache: dict | None = None
_settings_cache_time: float = 0
_SETTINGS_CACHE_TTL = 5.0
_settings_lock = threading.Lock()

# 第一版支持的设置项及默认值
DEFAULT_SETTINGS = {
    "download_root": str(DOWNLOAD_ROOT),
    "ui_theme": DEFAULT_UI_THEME,
    "ui_fx_enabled": DEFAULT_UI_FX_ENABLED,
    "ui_fx_preset": DEFAULT_UI_FX_PRESET,
    "max_running_jobs": "1",
    "timeout": "30",
    "retry_times": "2",
    "proxy": "",
    "client_type": "api",
    "skip_existing": "true",
    "image_format": "webp",
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
    # 界面：资源库卡片正文空白处点一下是否在新标签页打开详情（默认关，封面/标题/“详情”始终可以打开）
    "library_card_click": "false",
    "home_show_recent_downloads": "false",
    # 新章节检查：默认开；只查本地有已下载内容的漫画，只取章节列表，从不下载（core/update_checker.py）
    "auto_update_check": "true",
}


_ENUM_SETTINGS: dict[str, set[str]] = {
    "client_type": {"api", "html"},
    "pack_format": {"cbz", "zip"},
    "image_format": {"webp", "png", "jpg"},
    "organize_mode": {"none", "by_author", "flat"},
    "ui_theme": UI_THEMES,
    "ui_fx_preset": UI_FX_PRESETS,
}
_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "timeout": (5, 120),
    "retry_times": (0, 20),
    "max_running_jobs": (1, 5),
    "image_threads": (1, 50),
    "photo_threads": (1, 10),
    "schedule_start": (0, 23),
    "schedule_end": (0, 23),
}
_BOOL_KEYS: frozenset[str] = frozenset({
    "schedule_enabled",
    "auto_pack",
    "delete_originals",
    "skip_existing",
    "library_card_click",
    "auto_update_check",
    "home_show_recent_downloads",
    "ui_fx_enabled",
})


def merge_stored_settings(stored) -> dict:
    """将数据库读取到的原始键值与默认值合并并归一化（不访问数据库）。"""
    merged = dict(DEFAULT_SETTINGS)
    merged.update(stored)
    # A legacy database/import cannot relocate the process's storage roots.
    merged["download_root"] = str(DOWNLOAD_ROOT)
    merged["ui_theme"] = normalize_theme(merged.get("ui_theme"))
    merged["ui_fx_preset"] = normalize_fx_preset(merged.get("ui_fx_preset"))
    for key in ("client_type", "image_format", "pack_format"):
        val = str(merged.get(key, "")).strip().lower()
        merged[key] = val if val in _ENUM_SETTINGS[key] else DEFAULT_SETTINGS[key]
    organize_mode = str(merged.get("organize_mode", "")).strip()
    merged["organize_mode"] = organize_mode if organize_mode in _ENUM_SETTINGS["organize_mode"] else "none"
    for key, (lo, hi) in _INT_BOUNDS.items():
        merged[key] = str(_bounded_int(merged, key, lo, hi))
    for key in _BOOL_KEYS:
        val = str(merged.get(key, "")).strip().lower()
        merged[key] = val if val in {"true", "false"} else DEFAULT_SETTINGS[key]
    if merged["auto_pack"] != "true":
        merged["delete_originals"] = "false"
    return merged


def get_settings() -> dict:
    """获取所有设置（合并默认值），5 秒缓存"""
    global _settings_cache, _settings_cache_time
    now = time.time()
    with _settings_lock:
        if _settings_cache is not None and (now - _settings_cache_time) < _SETTINGS_CACHE_TTL:
            return dict(_settings_cache)
        merged = merge_stored_settings(db.get_all_settings())
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
    if key == "download_root":
        return str(DOWNLOAD_ROOT)
    return db.get_setting(key, DEFAULT_SETTINGS.get(key, ""))


def normalize_settings_patch(settings: dict) -> dict:
    """Validate a settings patch and normalize its values without touching the database."""
    normalized = {}
    for key, value in settings.items():
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"未知设置项: {key}")
        if key == "download_root":
            normalized[key] = str(DOWNLOAD_ROOT)
            continue
        if key in _BOOL_KEYS:
            if isinstance(value, bool):
                value = "true" if value else "false"
            if not isinstance(value, str) or value.strip().lower() not in {"true", "false"}:
                raise ValueError(f"{key} 必须为 true 或 false")
            normalized[key] = value.strip().lower()
        elif key in _INT_BOUNDS:
            lo, hi = _INT_BOUNDS[key]
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError(f"{key} 必须是 {lo}–{hi} 的整数")
            try:
                number = int(value)
            except (ValueError, TypeError):
                raise ValueError(f"{key} 必须是 {lo}–{hi} 的整数") from None
            if not lo <= number <= hi:
                raise ValueError(f"{key} 必须是 {lo}–{hi} 的整数")
            normalized[key] = str(number)
        elif key in _ENUM_SETTINGS:
            if not isinstance(value, str) or value.strip() not in _ENUM_SETTINGS[key]:
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
    return normalized


def update_settings(settings: dict) -> set[str]:
    """Commit a validated patch atomically and return the settings whose values changed."""
    normalized = normalize_settings_patch(settings)
    if not normalized:
        return set()
    with db.transaction() as conn:
        stored = dict(conn.execute("SELECT key, value FROM settings"))
        old_auto_pack = stored.get("auto_pack", DEFAULT_SETTINGS["auto_pack"])
        if normalized.get("auto_pack", old_auto_pack) != "true":
            normalized["delete_originals"] = "false"
        elif old_auto_pack != "true":
            normalized.setdefault("delete_originals", "false")
        changed = {key for key, value in normalized.items() if stored.get(key, DEFAULT_SETTINGS[key]) != value}
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            [(key, value) for key, value in normalized.items() if stored.get(key) != value],
        )
    invalidate_settings_cache()
    return changed


def _bounded_int(settings: dict, key: str, lo: int, hi: int) -> int:
    raw = settings.get(key)
    if not isinstance(raw, bool):
        try:
            value = int(raw)
            if lo <= value <= hi:
                return value
        except (ValueError, TypeError):
            pass
    return int(DEFAULT_SETTINGS[key])


def build_jmcomic_option(settings: dict | None = None) -> dict:
    """根据当前设置或调用方传入的完整快照生成 jmcomic option 配置字典。"""
    from curl_cffi import CurlOpt
    s = get_settings() if settings is None else settings
    proxy = str(s.get("proxy") or "").strip()
    timeout = _bounded_int(s, "timeout", 5, 120)
    fmt = str(s.get("image_format") or "webp").strip().lower()
    image_suffix = {"webp": ".webp", "png": ".png", "jpg": ".jpg", "jpeg": ".jpg"}.get(fmt, ".webp")
    skip_existing = str(s.get("skip_existing", "true")).strip().lower() != "false"

    option = {
        "download": {
            "cache": skip_existing,
            "image": {
                "suffix": image_suffix,
            },
            "threading": {
                "image": _bounded_int(s, "image_threads", 1, 50),
                "photo": _bounded_int(s, "photo_threads", 1, 10),
            },
        },
        "client": {
            "impl": "html",
            "retry_times": _bounded_int(s, "retry_times", 0, 20),  # 降为 2，网络稳定时减少重试等待
            "timeout": timeout,
            # 由 jmcomic 按 impl 选择其维护的域名；API 地址不能用于 HTML 客户端。
            # 不覆盖 domain，升级依赖时即可同步 API 分流地址和协议版本。
            # ⭐ 启用 session 模式，复用 TCP 连接，消除每次请求的 TLS 握手
            "postman": {
                "type": "curl_cffi_session",
                "meta_data": {
                    "impersonate": "chrome",
                    "proxies": {"http": proxy, "https": proxy} if proxy else {},
                    "curl_options": {CurlOpt.NOPROXY: "" if proxy else "*"},
                    # ⭐ 显式设置 HTTP 请求超时（秒），防止 curl_easy_perform 无限阻塞
                    "timeout": timeout,
                },
            },
        },
        "dir_rule": {
            "base_dir": str(DOWNLOAD_ROOT),
        },
    }

    client_type = str(s.get("client_type") or DEFAULT_SETTINGS["client_type"]).strip().lower()
    option["client"]["impl"] = "html" if client_type == "html" else "api"

    return option
