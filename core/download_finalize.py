"""Organize completed downloads, merge their archives and clean up verified originals."""
import shutil
from pathlib import Path

from . import archive_pages, flat_pages
from .file_tree import is_link
from .local_availability import forget
from .logger import log
from .packer import CbzPacker
from .path_guard import is_safe_path
from .transfer import DownloadInterrupted
from .validation import safe_dirname


def organize_download(output_path, mode, album, *, photos=None, flat_originals=None, check_control=lambda: None):
    if mode not in ("by_author", "flat"):
        return None
    directory = Path(output_path)
    try:
        check_control()
        if not is_safe_path(directory) or not directory.is_dir() or is_link(directory):
            raise ValueError("下载目录不存在或不安全")
        if mode == "flat":
            confirmed = flat_pages.flatten(directory, album if photos is None else photos, check_control)
            if flat_originals is not None:
                flat_originals.update(confirmed)
            log.info(f"扁平整理完成：确认 {len(confirmed)} 页，保留其他文件")
            return str(directory)
        author = safe_dirname(str(getattr(album, "author", "") or "unknown_author"))
        destination = directory.parent / author / directory.name
        if destination.exists() or destination.is_relative_to(directory):
            log.warning(f"整理目标已存在或位于源目录内部，保留源目录: {destination}")
            return None
        if not is_safe_path(destination):
            raise ValueError("整理目标路径越权")
        destination.parent.mkdir(parents=True, exist_ok=True)
        check_control()
        shutil.move(str(directory), str(destination))
        return str(destination)
    except DownloadInterrupted:
        raise
    except Exception as error:
        log.warning(f"整理目录失败 mode={mode} path={output_path} error={error}")
        return None


def _archive_bases(directory, target, check_control):
    bases = []
    for path in archive_pages.candidates(directory):
        check_control()
        index = archive_pages.read_index(path)
        replaced = path.name.casefold() == target.name.casefold()
        if index.transient or index.skipped or (replaced and index.status == "corrupt"):
            raise ValueError(f"旧压缩包 {path.name} 暂时打不开或有读不了的页（{index.status}，"
                             f"跳过 {index.skipped} 页），没有打包，原图保留")
        if index.status in ("ok", "empty"):
            bases.append(index)
    return bases


def _signature(path):
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _pack(directory, settings, check_control, flat_originals):
    check_control()
    packer = CbzPacker()
    suffix = ".zip" if settings.get("pack_format") == "zip" else packer.extension()
    target = directory / f"{directory.name}{suffix}"
    bases = _archive_bases(directory, target, check_control)
    files = packer.packable_images(directory)
    if flat_originals is not None:
        flat_pages.check_unowned_overlaps(directory, files, flat_originals, bases, check_control)
    originals = {path: _signature(path) for path in files if flat_originals is None or path in flat_originals}
    packer.pack(directory, target, base=bases, check_control=check_control)
    check_control()
    check = archive_pages.read_index(target)
    if check.status not in ("ok", "empty") or check.skipped or len(check.pages) != packer.packed_count:
        raise ValueError(f"新压缩包 {target.name} 核对不通过（{check.status}，{len(check.pages)}/"
                         f"{packer.packed_count} 页），原图保留")
    if settings.get("delete_originals") == "true" and is_safe_path(directory):
        packed = {page.name for page in check.pages}
        for path, signature in originals.items():
            if (path.relative_to(directory).as_posix() in packed and is_safe_path(path)
                    and path.is_file() and not is_link(path) and _signature(path) == signature):
                check_control()
                path.unlink()


def finalize_files(output_path, mode, album, photos, *, keep_location, settings, check_control,
                   path_changed, archiving):
    """Report moves immediately; optional packing failure keeps a completed download readable."""
    flat_originals = set() if mode == "flat" else None
    if mode == "by_author" and keep_location:
        log.info(f"复用成功资源目录，保留原位置: {output_path}")
    elif mode in ("by_author", "flat"):
        organized = organize_download(output_path, mode, album, photos=photos, flat_originals=flat_originals,
                                      check_control=check_control)
        if organized and organized != output_path:
            output_path = organized
            path_changed(output_path)
        if mode == "flat" and organized is None:
            return output_path
    if settings.get("auto_pack") == "true":
        directory = Path(output_path)
        try:
            check_control()
            archiving()
            _pack(directory, settings, check_control, flat_originals)
        except DownloadInterrupted:
            raise
        except Exception as error:
            log.warning(f"CBZ 打包失败（不影响下载完成状态）path={directory} error={error}")
        finally:
            forget(directory)
    return output_path
