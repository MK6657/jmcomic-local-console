"""
核心验证工具模块

集中管理所有跨路由共享的校验函数、正则、常量。
避免各 route 文件重复定义相同逻辑。

包含：
  - album_id 纯数字校验（SSRF 保护）
  - job_id 格式校验
  - 图片类型常量（ALLOWED_EXTENSIONS / IMAGE_MIME_MAP）
  - 文件名安全工具
  - 通用参数校验
"""

import re
from typing import Optional

# ============================================================
# album_id 纯数字校验（SSRF 保护）
# 使用场景：api_album / api_jobs / api_library / api_wishlist
# ============================================================
_RE_NUMERIC = re.compile(r"^\d+$")


def validate_numeric(album_id: str) -> bool:
    """校验 album_id 是否为纯数字（SSRF 保护）"""
    return bool(_RE_NUMERIC.match(album_id))


def require_numeric(album_id: str, field_name: str = "album_id") -> Optional[str]:
    """校验 album_id 为纯数字，合法时返回 None，非法时返回错误消息"""
    if not _RE_NUMERIC.match(album_id):
        return f"{field_name} 必须是纯数字"
    return None


# ============================================================
# job_id 格式校验
# 使用场景：api_export / api_jobs
# ============================================================
_RE_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validate_job_id(job_id: str) -> bool:
    """校验 job_id 格式：字母数字下划线连字符，长度 1-64"""
    return bool(_RE_JOB_ID.match(job_id))


def require_job_id(job_id: str) -> Optional[str]:
    """校验 job_id，非法时返回错误消息，合法返回 None"""
    if not _RE_JOB_ID.match(job_id):
        return "job_id 格式非法"
    return None


# ============================================================
# 图片类型常量
# 使用场景：api_album（封面MIME）/ api_export（PDF扫描）/ api_preview（图片类型校验）
# ============================================================

# 预览/封面场景支持的扩展名（最精简版本）
ALLOWED_EXTENSIONS = {".webp", ".jpg", ".jpeg", ".png", ".gif"}

# 导出/转换场景（PDF / img2pdf 需要支持更多格式）
EXPORT_IMAGE_EXTENSIONS = {".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".avif"}

# MIME 类型映射（统一来源，避免各文件分别定义不一致）
IMAGE_MIME_MAP = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
}


def is_allowed_image(suffix: str) -> bool:
    """检查文件后缀是否在预览允许的图片类型中"""
    return suffix.lower() in ALLOWED_EXTENSIONS


def get_image_mime(suffix: str, fallback: str = "application/octet-stream") -> str:
    """根据后缀返回对应 MIME 类型，未知后缀返回 fallback"""
    return IMAGE_MIME_MAP.get(suffix.lower(), fallback)


# ============================================================
# 文件名安全工具
# ============================================================

# 非法文件名字符（Windows 不允许出现在文件名中的字符）
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')

# Windows 保留名（不区分大小写）
_WINDOWS_RESERVED: set[str] = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_filename(name: str, max_len: int = 200) -> str:
    """清理文件名中的非法字符，返回安全的文件名。

    不处理 Windows 保留名（适用于文件扩展名后的普通文件名）。
    若结果为空，返回 "export"。
    """
    name = _INVALID_FILENAME_CHARS.sub("_", name)
    name = name.rstrip(". ")  # Windows 尾部 . 和空格行为不一致
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    name = name.strip()
    return name or "export"


def safe_dirname(name: str, max_len: int = 200) -> str:
    """清理目录名中的非法字符，处理 Windows 保留名。

    比 safe_filename 更严格：检测 CON / PRN / AUX / NUL / COM1-9 / LPT1-9，
    并在前缀加 '_' 避免文件系统冲突。
    若结果为空，生成一个随机名称。
    """
    import uuid

    name = _INVALID_FILENAME_CHARS.sub("_", name)
    name = name.rstrip(". ")
    # 检测 Windows 保留名
    stem = name.split(".")[0].lower()
    if stem in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    name = name.strip()
    if not name:
        name = f"untitled_{uuid.uuid4().hex[:8]}"
    return name


# ============================================================
# 通用参数校验
# ============================================================

def clamp_page(page: int, min_val: int = 1, max_val: int = 500) -> int:
    """限制 page 参数在合法范围内"""
    return max(min_val, min(page, max_val))


def clamp_page_size(size: int, min_val: int = 1, max_val: int = 200) -> int:
    """限制 page_size 参数在合法范围内"""
    return max(min_val, min(size, max_val))


def truncate_str(value: str, max_len: int = 200) -> str:
    """截断字符串到指定最大长度"""
    return value[:max_len] if len(value) > max_len else value
