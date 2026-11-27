"""Aspect-ratio-preserving local cover thumbnail cache.

Serves lightweight WebP thumbnails for local comic covers when requested with
`?w=<px>`, ensuring thumbnails stay 100% visually identical to the comic's
first page (zero cropping, zero upscaling) while reducing browser decode RAM
and local transfer overhead.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
import hashlib
import io
import os
import tempfile
import threading
import time
from pathlib import Path

from .file_tree import is_link
from .path_utils import get_cache_dir

ALLOWED_WIDTHS: tuple[int, ...] = (180, 360, 480, 600)
DEFAULT_PORTRAIT_WIDTH = 480
MAX_CACHED_THUMBS = 400
PRUNE_TARGET = 320
WEBP_QUALITY = 82
MAX_SOURCE_PIXELS = 64_000_000
_MEM_CACHE_MAX = 500
_CacheKey = tuple[Path, str, int]
_mem_lock = threading.Lock()
_mem_hits: OrderedDict[_CacheKey, Path | None] = OrderedDict()
_in_flight: dict[_CacheKey, Future] = {}
_encode_semaphore = threading.BoundedSemaphore(4)


def parse_width(raw: str | None) -> int | None:
    """Snap an optional requested width to a bounded discrete bucket."""
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    for bucket in ALLOWED_WIDTHS:
        if value <= bucket:
            return bucket
    return ALLOWED_WIDTHS[-1]


_prune_lock = threading.Lock()


def _prune_if_needed(folder: Path) -> None:
    if not _prune_lock.acquire(blocking=False):
        return
    try:
        candidates = []
        now = time.time()
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    entry_path = Path(entry.path)
                    if is_link(entry_path):
                        continue
                    if entry.name.startswith(".thumb-") and entry.name.endswith(".tmp"):
                        if now - st.st_mtime > 300.0:
                            entry_path.unlink(missing_ok=True)
                        continue
                    if not entry.name.endswith(".webp"):
                        continue
                    candidates.append((st.st_mtime, entry_path))
                except OSError:
                    continue
        if len(candidates) <= MAX_CACHED_THUMBS:
            return
        with _mem_lock:
            hot_paths = {v for v in list(_mem_hits.values())[-160:] if isinstance(v, Path)}
        candidates.sort(key=lambda item: (item[1] in hot_paths, item[0]))
        to_remove = len(candidates) - PRUNE_TARGET
        removed = []
        for mtime, stale in candidates:
            if len(removed) >= to_remove:
                break
            # Keep freshly written thumbnails so concurrent send_file() calls never hit ENOENT
            if now - mtime < 10.0:
                continue
            try:
                stale.unlink(missing_ok=True)
                removed.append(stale)
            except OSError:
                continue
        if removed:
            dead = set(removed)
            with _mem_lock:
                for key in [k for k, v in _mem_hits.items() if v in dead]:
                    _mem_hits.pop(key, None)
    except OSError:
        pass
    finally:
        _prune_lock.release()


def _encode_thumbnail(source_bytes: bytes | None, source_path: Path | None, width: int) -> bytes | None:
    """Return aspect-preserving WebP bytes when downscaling helps, else None."""
    from PIL import Image, ImageOps

    stream = io.BytesIO(source_bytes) if source_bytes is not None else None
    target = stream if stream is not None else source_path
    if target is None:
        return None
    max_height = width * 2
    with _encode_semaphore, Image.open(target) as raw_img:
        if raw_img.width * raw_img.height > MAX_SOURCE_PIXELS:
            return None
        orientation = 1
        try:
            orientation = int(raw_img.getexif().get(274, 1) or 1)
        except Exception:
            orientation = 1
        eff_w, eff_h = (raw_img.height, raw_img.width) if orientation in (5, 6, 7, 8) else raw_img.size
        if eff_w <= width and eff_h <= max_height:
            return None
        img = ImageOps.exif_transpose(raw_img) if orientation != 1 else raw_img
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        elif img is raw_img:
            img = img.copy()
        img.thumbnail((width, max_height), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
        return out.getvalue()


def _cache_path(folder: Path, cache_key: str, width: int) -> Path:
    digest = hashlib.sha256(f"v1:{cache_key}:{width}".encode("utf-8")).hexdigest()[:24]
    return folder / f"{digest}.webp"


def _valid_thumbnail(folder: Path, target: Path) -> bool:
    try:
        if target.parent != folder or is_link(folder) or is_link(target):
            return False
        st = target.stat()
        return st.st_size > 0 and target.is_file()
    except (OSError, RuntimeError):
        return False


def _remember_hit(key: _CacheKey, target: Path | None) -> None:
    with _mem_lock:
        _mem_hits[key] = target
        _mem_hits.move_to_end(key)
        while len(_mem_hits) > _MEM_CACHE_MAX:
            _mem_hits.popitem(last=False)


def _lookup_cached(key: _CacheKey) -> tuple[bool, Path | None]:
    folder = key[0]
    with _mem_lock:
        if key in _mem_hits:
            hit = _mem_hits[key]
            if hit is None or _valid_thumbnail(folder, hit):
                _mem_hits.move_to_end(key)
                return True, hit
            del _mem_hits[key]
    target = _cache_path(*key)
    if _valid_thumbnail(folder, target):
        _remember_hit(key, target)
        return True, target
    return False, None


def get_cached_thumbnail(*, cache_key: str, width: int) -> Path | None:
    """Return an existing cached thumbnail Path without reading the source image."""
    _, thumbnail = _lookup_cached((get_cache_dir() / "thumbs", cache_key, width))
    return thumbnail


def get_or_build_thumbnail(
    *,
    cache_key: str,
    width: int,
    source_path: Path | None = None,
    source_bytes: bytes | None = None,
) -> Path | bytes | None:
    """Return a cached thumbnail Path (or raw WebP bytes if disk cache unavailable).

    Returns None when the original image is already small enough or cannot be resized,
    signaling the caller to serve the original cover bytes unchanged.
    """
    key = (get_cache_dir() / "thumbs", cache_key, width)
    found, existing = _lookup_cached(key)
    if found:
        return existing
    if source_path is None and source_bytes is None:
        return None
    with _mem_lock:
        pending = _in_flight.get(key)
        leader = pending is None
        if leader:
            pending = _in_flight[key] = Future()
    if not leader:
        result = pending.result()
        if isinstance(result, Path) and not _valid_thumbnail(key[0], result):
            return None
        return result

    try:
        found, existing = _lookup_cached(key)
        result = existing if found else _build_thumbnail(key, source_bytes, source_path)
        pending.set_result(result)
        return result
    except BaseException as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _mem_lock:
            del _in_flight[key]


def _build_thumbnail(key: _CacheKey, source_bytes: bytes | None, source_path: Path | None) -> Path | bytes | None:
    try:
        data = _encode_thumbnail(source_bytes, source_path, key[2])
    except Exception:
        return None
    if data is None:
        _remember_hit(key, None)
        return None
    folder = key[0]
    target = _cache_path(*key)
    tmp_path = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if is_link(folder):
            return data
        fd, tmp_name = tempfile.mkstemp(prefix=".thumb-", suffix=".tmp", dir=str(folder))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, target)
        tmp_path = None
        _prune_if_needed(folder)
        if _valid_thumbnail(folder, target):
            _remember_hit(key, target)
            return target
    except OSError:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return data
