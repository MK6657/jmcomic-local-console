"""Flatten only marked chapters and their original page numbers. No on-disk index."""
import hashlib
import os
import re
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path

from . import archive_pages
from .download_layout import chapter_output_dir
from .file_tree import is_link
from .logger import log
from .path_guard import is_safe_path
from .validation import EXPORT_IMAGE_EXTENSIONS, safe_dirname

_FLAT_PAGE = re.compile(r"__([0-9]{1,20})_p([0-9]{5,})(?:~[0-9]+)?\Z")


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


@dataclass
class _Variant:
    name: str
    loose: list = field(default_factory=list)
    packed: list = field(default_factory=list)

    def matches(self, size, digest, read):
        for path in self.loose:
            if (path.suffix.lower() != ".webp" or is_link(path) or not path.is_file()
                    or path.stat().st_size != size or _digest(path) != digest):
                return False
        for index, page in self.packed:
            if (page.suffix != ".webp" or page.size != size
                    or hashlib.sha256(read(index, page)).digest() != digest):
                return False
        return True


class _FlatPages:
    def __init__(self, directory, stack, check_control):
        self.directory = directory
        self.stack = stack
        self.readers = {}
        self.variants = defaultdict(dict)
        self.check_control = check_control
        for path in directory.iterdir():
            variant = self._variant(path.name)
            if variant:
                variant.loose.append(path)
        for path in archive_pages.candidates(directory):
            check_control()
            index = archive_pages.read_index(path)
            if index.transient or index.skipped or index.status == "corrupt":
                raise ValueError(f"不能确认旧包 {path.name} 里的页，保留章节原图")
            for page in index.pages:
                if not page.chapter:
                    variant = self._variant(page.name)
                    if variant:
                        variant.packed.append((index, page))

    def _variant(self, name):
        path = Path(name)
        match = _FLAT_PAGE.search(path.stem)
        if (match and path.suffix.lower() in EXPORT_IMAGE_EXTENSIONS and path.name == name
                and name.isprintable() and safe_dirname(name, max_len=255) == name):
            return self.variants[match.groups()].setdefault(path.stem.casefold(), _Variant(name))
        return None

    def _read(self, index, page):
        if index.path not in self.readers:
            self.readers[index.path] = self.stack.enter_context(archive_pages.open_pages(index))
        return self.readers[index.path](page)

    def place(self, source, prefix, photo_id, *, verify_source=None):
        variants = self.variants[photo_id, source.stem]
        if variants:
            size, digest = source.stat().st_size, _digest(source)
            for variant in variants.values():
                self.check_control()
                if variant.matches(size, digest, self._read):
                    if variant.loose:
                        self.check_control()
                        if verify_source:
                            verify_source()
                        source.unlink()
                        return variant.loose[0]
                    return self._move(source, self.directory / variant.name, variant, verify_source)
            log.warning(f"扁平整理：章节 {photo_id} 第 {source.stem} 页已有不同内容，分别保留")
        for version in count(1):
            suffix = "" if version == 1 else f"~{version}"
            target = self.directory / f"{prefix}_p{source.stem}{suffix}.webp"
            if target.stem.casefold() not in variants and not os.path.lexists(target):
                return self._move(source, target, self._variant(target.name), verify_source)

    def _move(self, source, target, variant, verify_source):
        if variant is None or target.parent != self.directory or not is_safe_path(target):
            raise ValueError("扁平整理目标名称不安全，保留原图")
        self.check_control()
        if verify_source:
            verify_source()
        if os.name == "nt":
            source.rename(target)
        else:
            os.link(source, target)
            self.check_control()
            source.unlink()
        variant.loose.append(target)
        return target


def flatten(directory, photos, check_control):
    """Return only confirmed flat files eligible for cleanup after a verified pack."""
    confirmed = set()
    with ExitStack() as stack:
        flat = _FlatPages(directory, stack, check_control)
        for photo in photos:
            check_control()
            folder = chapter_output_dir(directory, photo, create=False)
            if folder is None:
                continue
            photo_id = str(photo.photo_id)
            prefix = f"{folder.name.rsplit('__', 1)[0]}__{photo_id}"
            for number in range(1, len(photo) + 1):
                source = folder / f"{number:05d}.webp"
                if os.path.lexists(source) and not is_link(source) and source.is_file():
                    confirmed.add(flat.place(source, prefix.replace(" ", "_"), photo_id))
    return confirmed


def check_unowned_overlaps(directory, loose, confirmed, bases, check_control):
    """Packer prefers loose files by path stem. An unowned loose file must not erase different archived bytes."""
    unowned = defaultdict(list)
    overlaps = {}
    for path in loose:
        if path not in confirmed:
            unowned[archive_pages.page_key(path.relative_to(directory).as_posix())].append(path)
    for index in bases:
        if not unowned:
            break
        with ExitStack() as stack:
            read = None
            for page in index.pages:
                paths = unowned.get(archive_pages.page_key(page.name))
                if paths:
                    check_control()
                    if read is None:
                        read = stack.enter_context(archive_pages.open_pages(index))
                    archived = hashlib.sha256(read(page)).digest()
                    if any(path.stat().st_size != page.size or _digest(path) != archived for path in paths):
                        raise ValueError(f"旧包与未认领散图 {page.name} 内容不同，保留旧包和原图")
                    overlaps.update(dict.fromkeys(paths, archived.hex()))
    return overlaps
