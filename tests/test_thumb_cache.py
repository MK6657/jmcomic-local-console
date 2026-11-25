from concurrent.futures import Future, ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

from PIL import Image

from core import thumb_cache


def _source(width, height):
    buffer = BytesIO()
    Image.new("RGB", (width, height), "green").save(buffer, format="PNG")
    return buffer.getvalue()


def test_four_concurrent_cold_thumbnails_share_one_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(thumb_cache, "get_cache_dir", lambda: tmp_path)
    encoder = thumb_cache._encode_thumbnail
    all_waiting = Barrier(4)

    class JoinedFuture(Future):
        def result(self, timeout=None):
            all_waiting.wait(timeout=5)
            return super().result(timeout=timeout)

    def encode(*args):
        all_waiting.wait(timeout=5)
        return encoder(*args)

    encoding = Mock(side_effect=encode)
    monkeypatch.setattr(thumb_cache, "Future", JoinedFuture)
    monkeypatch.setattr(thumb_cache, "_encode_thumbnail", encoding)
    payload = _source(480, 960)

    with ThreadPoolExecutor(max_workers=4) as pool:
        requests = [pool.submit(thumb_cache.get_or_build_thumbnail, cache_key="cover",
                                width=180, source_bytes=payload) for _ in range(4)]
        paths = [future.result(timeout=8) for future in requests]
    encoding.assert_called_once()
    assert all(isinstance(path, Path) and path == paths[0] for path in paths)
    with Image.open(paths[0]) as image:
        assert image.size == (180, 360)


def test_small_thumbnail_is_examined_once_per_version_and_width(tmp_path, monkeypatch):
    monkeypatch.setattr(thumb_cache, "get_cache_dir", lambda: tmp_path)
    encoding = Mock(wraps=thumb_cache._encode_thumbnail)
    monkeypatch.setattr(thumb_cache, "_encode_thumbnail", encoding)
    payload = _source(60, 70)

    for _ in range(4):
        assert thumb_cache.get_or_build_thumbnail(cache_key="cover-v1", width=180, source_bytes=payload) is None
    encoding.assert_called_once()
    assert thumb_cache.get_or_build_thumbnail(cache_key="cover-v1", width=360, source_bytes=payload) is None
    assert thumb_cache.get_or_build_thumbnail(cache_key="cover-v2", width=180, source_bytes=payload) is None
    assert encoding.call_count == 3


def test_regular_thumbnail_is_reused_from_disk_and_memory_in_its_cache_root(tmp_path, monkeypatch):
    root = tmp_path / "first"
    monkeypatch.setattr(thumb_cache, "get_cache_dir", lambda: root)
    options = {"cache_key": "cover", "width": 180}
    thumbnail = thumb_cache.get_or_build_thumbnail(**options, source_bytes=_source(480, 840))
    assert thumbnail is not None and thumbnail.parent == root / "thumbs"

    with thumb_cache._mem_lock:
        thumb_cache._mem_hits.pop((root / "thumbs", "cover", 180))
    assert thumb_cache.get_cached_thumbnail(**options) == thumbnail
    assert thumb_cache.get_cached_thumbnail(**options) == thumbnail
    with monkeypatch.context() as patch:
        inspect_link = thumb_cache.is_link
        patch.setattr(thumb_cache, "is_link", lambda path: Path(path) == thumbnail or inspect_link(path))
        assert thumb_cache.get_cached_thumbnail(**options) is None
        assert thumb_cache.get_cached_thumbnail(**options) is None
    assert thumb_cache.get_cached_thumbnail(**options) == thumbnail
    root = tmp_path / "second"
    assert thumb_cache.get_cached_thumbnail(**options) is None
    root = tmp_path / "first"
    thumbnail.unlink()
    assert thumb_cache.get_cached_thumbnail(**options) is None


def test_prune_evicts_oldest_files_and_clears_memory_hits_without_error(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(thumb_cache, "get_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(thumb_cache, "MAX_CACHED_THUMBS", 3)
    monkeypatch.setattr(thumb_cache, "PRUNE_TARGET", 2)
    payload = _source(480, 840)

    built = []
    for idx in range(5):
        path = thumb_cache.get_or_build_thumbnail(cache_key=f"cover-{idx}", width=180, source_bytes=payload)
        assert isinstance(path, Path) and path.is_file()
        stamp = 1_700_000_000 + idx * 10
        os.utime(path, (stamp, stamp))
        built.append(path)

    thumb_cache._prune_if_needed(tmp_path / "thumbs")
    remaining = list((tmp_path / "thumbs").glob("*.webp"))
    assert len(remaining) <= 3 and len(remaining) < len(built)
    with thumb_cache._mem_lock:
        for hit_path in thumb_cache._mem_hits.values():
            if hit_path is not None:
                assert hit_path.exists()
