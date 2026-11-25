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

from functools import lru_cache
import os
from pathlib import Path
import re
from typing import Optional

# ============================================================
# album_id 纯数字校验（SSRF 保护）与车号提取
# 使用场景：api_album / api_jobs / api_library / api_wishlist
# ============================================================
_RE_NUMERIC = re.compile(r"[0-9]{1,20}")
_RE_JM_TOKEN = re.compile(r"^(?:jm[\s/_-]*)?([0-9]{1,20})$", re.IGNORECASE)
_RE_ALBUM_URL = re.compile(
    r"^(?:https?://)?(?:[\w-]+\.)*(?:18comic|jmcomic|jm-comic)[\w.-]*/album/([0-9]{1,20})(?:[/?#].*)?$",
    re.IGNORECASE,
)


def validate_numeric(album_id: str) -> bool:
    """校验 album_id 是否为纯数字（SSRF 保护）"""
    return isinstance(album_id, str) and bool(_RE_NUMERIC.fullmatch(album_id))


def extract_album_id(raw: object) -> Optional[str]:
    """从纯数字、JM123456 / JM / 123456 或禁漫专辑 URL 中提取纯数字车号；普通关键词返回 None。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if _RE_NUMERIC.fullmatch(text):
        return text
    match = _RE_JM_TOKEN.fullmatch(text) or _RE_ALBUM_URL.fullmatch(text)
    if match:
        candidate = match.group(1)
        if validate_numeric(candidate):
            return candidate
    return None


def parse_numeric_id_list(
    raw_ids: object,
    *,
    max_items: int,
    allow_empty: bool = False,
    field_name: str = "album_ids",
) -> tuple[Optional[list[str]], Optional[str]]:
    """统一校验纯数字 ID 列表，返回 (ids, error_message)。"""
    if not isinstance(raw_ids, list) or (not allow_empty and not raw_ids):
        return None, f"缺少 {field_name} 列表"
    if len(raw_ids) > max_items:
        return None, f"{field_name} 数量过多，最大 {max_items}"
    cleaned: list[str] = []
    for item in raw_ids:
        if isinstance(item, bool):
            return None, f"{field_name} 只能包含纯数字"
        text = str(item).strip() if isinstance(item, (str, int)) else ""
        if not validate_numeric(text):
            return None, f"{field_name} 只能包含纯数字"
        cleaned.append(text)
    return cleaned, None


def require_numeric(album_id: str, field_name: str = "album_id") -> Optional[str]:
    """校验 album_id 为纯数字，合法时返回 None，非法时返回错误消息"""
    if not validate_numeric(album_id):
        return f"{field_name} 必须是纯数字"
    return None


# ============================================================
# job_id 格式校验
# 使用场景：api_export / api_jobs
# ============================================================
_RE_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validate_job_id(job_id: str) -> bool:
    """校验 job_id 格式：字母数字下划线连字符，长度 1-64"""
    return isinstance(job_id, str) and bool(_RE_JOB_ID.fullmatch(job_id))


def require_job_id(job_id: str) -> Optional[str]:
    """校验 job_id，非法时返回错误消息，合法返回 None"""
    if not validate_job_id(job_id):
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
_INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f\ud800-\udfff<>:"/\\|?*]')

# Windows 保留名（不区分大小写）
_WINDOWS_RESERVED: set[str] = {
    "con", "prn", "aux", "nul", "conin$", "conout$",
    *(prefix + digit for prefix in ("com", "lpt") for digit in "123456789¹²³"),
}


def _safe_component(name: str, max_len: int) -> str:
    if max_len < 1:
        raise ValueError("max_len 必须为正数")
    limit = min(max_len, 255) * 2
    def truncate(text):
        return text.encode("utf-16-le")[:limit].decode("utf-16-le", errors="ignore").rstrip(". ")
    clean = truncate(_INVALID_FILENAME_CHARS.sub("_", name).strip())
    if clean.partition(".")[0].rstrip(" ").casefold() in _WINDOWS_RESERVED:
        clean = truncate("_" + clean)
    return clean


def safe_filename(name: str, max_len: int = 200) -> str:
    """清理 Windows 文件名（包括带扩展名的设备名）。"""
    return _safe_component(name, max_len) or "export"[:max_len]


def safe_dirname(name: str, max_len: int = 200) -> str:
    """清理 Windows 目录名；空名称使用随机后缀。"""
    import uuid
    return _safe_component(name, max_len) or f"untitled_{uuid.uuid4().hex[:8]}"[:max_len]


@lru_cache(maxsize=1)
def legacy_windows_paths() -> bool:
    """Ask about this process (both its manifest and system policy matter)."""
    if os.name != 'nt':
        return False
    import ctypes
    try:
        enabled = ctypes.WinDLL('ntdll').RtlAreLongPathsEnabled
        enabled.restype = ctypes.c_ubyte  # BOOLEAN, not BOOL
        return not enabled()
    except (AttributeError, OSError):  # older Windows has no opt-in
        return True


def path_units(path) -> int:
    return len(str(path).encode('utf-16-le', errors='surrogatepass')) // 2


def directory_budget(parent, *leaves, archive=False) -> int:
    """Available UTF-16 units for NAME in parent/NAME/<relative file>.

    On legacy Win32 reserve the terminator for files and 8.3 creation headroom
    for directories. An archive repeats NAME in its own filename.
    """
    if not legacy_windows_paths() or (os.name == 'nt' and str(parent).startswith('\\\\?\\')):
        return 251 if archive else 255
    slot = Path(parent).absolute() / 'x'
    budget = min(255, 248 - path_units(slot) + 1)
    for leaf in leaves:
        file = slot / leaf
        budget = min(budget, 259 - path_units(file) + 1, 248 - path_units(file.parent) + 1)
    if archive:
        budget = min(budget, (259 - path_units(slot / 'x.cbz') + 2) // 2)
    return budget


def short_root_error():
    return ValueError(r'下载路径过长，无法保留完整名称标识或创建暂存文件。请将下载根目录 JMCONSOLE_DOWNLOAD_DIR 改为较短的路径（例如 D:\JM）后重试。')


def fit_dirname(title, suffix, budget, *, archive=False):
    """Shorten only the already sanitized display text; preserve the identity suffix."""
    available = min(budget, 255) - path_units(suffix)
    if available < 0 or (not suffix and available < 1):
        raise short_root_error()
    text = _safe_component(title, available) if available else ''
    if os.name != 'nt':  # POSIX NAME_MAX counts bytes, including any later archive extension.
        room = 255 - len(os.fsencode(suffix)) - (4 if archive else 0)
        text = text.encode('utf-8')[:room].decode('utf-8', errors='ignore').rstrip('. ')
    return (text or ('' if suffix else '_')) + suffix


# ============================================================
# 通用参数校验
# ============================================================

def clamp_page(page: int, min_val: int = 1, max_val: int = 500) -> int:
    """限制 page 参数在合法范围内"""
    return max(min_val, min(page, max_val))


def clamp_page_size(size: int, min_val: int = 1, max_val: int = 200) -> int:
    """限制 page_size 参数在合法范围内"""
    return max(min_val, min(size, max_val))


def is_safe_upload_filename(filename: str) -> bool:
    """校验上传文件名：拒绝路径分隔符、控制字符、超长名称及 Windows 保留设备名。"""
    if not isinstance(filename, str):
        return False
    cleaned = filename.strip()
    if not cleaned or len(cleaned) > 255 or cleaned in {".", ".."} or cleaned.endswith("."):
        return False
    if any(ch in "/\\:" or ord(ch) < 32 or ord(ch) == 127 for ch in filename):
        return False
    stem = cleaned.split(".", 1)[0].strip().casefold()
    return stem not in _WINDOWS_RESERVED


def truncate_str(value: str, max_len: int = 200) -> str:
    """截断字符串到指定最大长度"""
    return value[:max_len] if len(value) > max_len else value
