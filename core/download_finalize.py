"""Organize completed downloads, merge their archives and clean up verified originals."""
from contextlib import ExitStack
import re
import shutil
from pathlib import Path

from . import archive_pages, flat_pages
from .backup_paths import safe_parts
from .download_layout import _marker
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


def _matches_publication(path, digest, signature):
    return (isinstance(digest, str) and is_safe_path(path) and not is_link(path.parent)
            and not is_link(path) and path.is_file() and path.stat().st_nlink == 1
            and _signature(path) == signature and flat_pages._digest(path).hex() == digest
            and _signature(path) == signature)


def _pack(directory, settings, check_control, confirmed=None, *, publication_proofs=None):
    check_control()
    packer = CbzPacker()
    suffix = ".zip" if settings.get("pack_format") == "zip" else packer.extension()
    target = directory / f"{directory.name}{suffix}"
    bases = _archive_bases(directory, target, check_control)
    files = packer.packable_images(directory)
    options = {}
    if publication_proofs is not None:
        confirmed = set()
        for path, (digest, signature) in publication_proofs.items():
            check_control()
            if _matches_publication(path, digest, signature):
                confirmed.add(path)
    if confirmed is not None:
        overlaps = flat_pages.check_unowned_overlaps(directory, files, confirmed, bases, check_control)
        if publication_proofs is not None:
            options['verified_inputs'] = {p: publication_proofs[p][0] if p in confirmed else overlaps.get(p)
                                          for p in files}
    originals = {path: _signature(path) for path in files if confirmed is None or path in confirmed}
    packer.pack(directory, target, base=bases, check_control=check_control, **options)
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
                if publication_proofs is None or _matches_publication(path, *publication_proofs[path]):
                    path.unlink()


def _published_sources(directory, published_pages, check_control):
    for photo_id, (folder, digests) in published_pages.items():
        check_control()
        if not folder:
            continue
        safe_parts([folder], context='公开章节')
        chapter = directory / folder
        if _marker(chapter).get('photo_id') != photo_id:
            continue
        for name, digest in digests.items():
            check_control()
            if not re.fullmatch(r'(?:[0-9]{5}|[1-9][0-9]{5,})\.webp', name) or name == '00000.webp':
                continue
            path = chapter / name
            if path.is_file() and not is_link(path):
                yield path, photo_id, (digest, _signature(path))


def finalize_published(directory, settings, check_control, save_progress, published_pages):
    """A name, page count, chapter marker or past flat receipt alone never permits removal."""
    packing = settings.get('auto_pack') == 'true'
    flatten = settings.get('organize_mode') == 'flat'
    if not (packing or flatten):
        return
    sources = _published_sources(directory, published_pages, check_control)
    proofs = {}
    if flatten:
        sources = [(path, pid, proof) for path, pid, proof in sources if _matches_publication(path, *proof)]
        cleanup = packing and settings.get('delete_originals') == 'true'
        if cleanup:
            receipts = settings.setdefault('_flat_sources', {})
            if sources:
                for source, photo_id, (_, signature) in sources:
                    receipts[f'{photo_id}:{source.stem}'] = list(signature)
                save_progress()
            for path in directory.iterdir():
                check_control()
                match = re.search(r'__([0-9]+)_p([0-9]{5,})(?:~[0-9]+)?\.webp\Z', path.name)
                if match and match[1] in published_pages:
                    digest = published_pages[match[1]][1].get(match[2] + '.webp')
                    signature = tuple(receipts.get(':'.join(match.groups()), ()))
                    if _matches_publication(path, digest, signature):
                        proofs[path] = (digest, signature)
        with ExitStack() as stack:
            flat = flat_pages._FlatPages(directory, stack, check_control)
            for source, photo_id, proof in sources:
                check_control()
                def verify_source():
                    if not _matches_publication(source, *proof):
                        raise ValueError('原图在扁平整理期间发生变化，保留原图')
                prefix = source.parent.name.rsplit('__', 1)[0].replace(' ', '_') + '__' + photo_id
                placed = flat.place(source, prefix, photo_id, verify_source=verify_source)
                # A pre-existing loose duplicate belongs to its owner even when identical.
                if _signature(placed) == proof[1]:
                    proofs[placed] = proof
    else:
        proofs = {source: proof for source, _, proof in sources}
    if packing:
        _pack(directory, settings, check_control, publication_proofs=proofs)
    settings.pop('_flat_sources', None)
    settings.pop('_flat_matches', None)


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
