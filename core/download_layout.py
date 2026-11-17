"""Identify the chapter directory used by both the page writer and file finalization."""
import json
import os
from pathlib import Path

from .file_tree import is_link
from .logger import log
from .path_guard import is_safe_path
from .storage import staged_file
from .validation import safe_dirname, validate_numeric

CHAPTER_MARKER = ".jm-chapter.json"
CHAPTER_FORMAT = 2


def _marker(directory):
    marker = directory / CHAPTER_MARKER
    try:
        if is_link(directory) or is_link(marker):
            return {}
        record = json.loads(marker.read_text(encoding="utf-8"))
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def chapter_output_dir(album_dir, photo, *, create=True):
    """Never adopt an unmarked folder. A read-only lookup follows the writer's exact suffix rules."""
    photo_id = str(photo.photo_id)
    if not validate_numeric(photo_id):
        raise ValueError("章节 ID 无效")
    title = safe_dirname(photo.name or photo_id, max_len=100)
    base = f"{title}__{photo_id}"
    suffix = 1
    while True:
        directory = Path(album_dir) / (base if suffix == 1 else f"{base}_{suffix}")
        marker = directory / CHAPTER_MARKER
        if not is_safe_path(directory) or not is_safe_path(marker):
            raise ValueError("章节输出路径越权")
        if os.path.lexists(directory):
            if _marker(directory).get("photo_id") == photo_id:
                return directory
            suffix += 1
            continue
        if not create:
            return None
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        marker.write_text(json.dumps({"photo_id": photo_id, "format": CHAPTER_FORMAT}), encoding="utf-8")
        return directory


def chapter_format(directory):
    value = _marker(Path(directory)).get("format", 1)
    return value if type(value) is int else 1


def mark_chapter_current(album_dir, photo):
    """Only upgrade the GIF format marker once every page has been confirmed."""
    try:
        directory = chapter_output_dir(album_dir, photo)
        if chapter_format(directory) < CHAPTER_FORMAT:
            with staged_file(directory / CHAPTER_MARKER) as staged:
                staged.path.write_text(json.dumps({"photo_id": str(photo.photo_id), "format": CHAPTER_FORMAT}),
                                       encoding="utf-8")
                staged.publish(lambda path: path.stat().st_size > 0)
    except (OSError, ValueError) as error:
        log.warning(f"更新章节目录标记失败 photo_id={photo.photo_id} error={error}")
