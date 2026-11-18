"""Per-export limits, shared by collection, extraction, conversion and output writes.

The deadline is cooperative: a Pillow decoder or img2pdf processing one page is
not killed in another thread. Checks before/after each page and every managed
write still prevent a late/oversized result from being published.
"""
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ExportLimits:
    files: int = 10_000
    pages: int = 2_000
    scan_entries: int = 50_000  # includes directories, empty and ignored files
    source_bytes: int = 2 * 1024 ** 3
    page_bytes: int = 128 * 1024 ** 2
    staging_bytes: int = 2 * 1024 ** 3
    output_bytes: int = 2 * 1024 ** 3
    page_pixels: int = 32_000_000
    archive_entries: int = 10_000
    archive_metadata_bytes: int = 8 * 1024 ** 2
    seconds: float = 300.0


DEFAULT_LIMITS = ExportLimits()


class ExportLimit(RuntimeError):
    """Resource budget exhausted; never publish the partial export."""


class ExportTimeout(RuntimeError):
    """The complete generation deadline expired."""


class ExportBudget:
    def __init__(self, limits=None, clock=None):
        self.limits = limits or DEFAULT_LIMITS
        self.clock = clock or time.monotonic
        self.deadline = self.clock() + self.limits.seconds
        self.scanned = self.files = self.pages = self.source_bytes = 0
        self.sources = {}
        self.page_digests = {}  # sealed task pages only; keyed by the actual collected source path
        self.archive_paths = []  # top-level archive candidates from the bounded scan
        self.input_sources = {}  # extracted path -> archive source; JPEG path -> None
        self.staging = {}
        self.staging_bytes = 0

    def check(self):
        if self.clock() >= self.deadline:
            raise ExportTimeout("导出超过总时间限制，已取消，请减少页数后重试")

    def visit(self):
        self.check()
        self.scanned += 1
        if self.scanned > self.limits.scan_entries:
            raise ExportLimit("下载目录条目过多，已取消导出")

    def check_size(self, size, is_page=False):
        self.check()
        if size < 0 or size > self.limits.source_bytes:
            raise ExportLimit("源文件超过导出大小限制")
        if is_page and size > self.limits.page_bytes:
            raise ExportLimit("单页图片超过导出大小限制")

    def add_source(self, key, size, is_page):
        self.check_size(size, is_page)
        if self.files + 1 > self.limits.files or self.pages + is_page > self.limits.pages:
            raise ExportLimit("导出文件或页数超过限制（最多 %s 个文件 / %s 页）" %
                              (self.limits.files, self.limits.pages))
        if self.source_bytes + size > self.limits.source_bytes:
            raise ExportLimit("源文件总大小超过导出限制")
        self.files += 1
        self.pages += bool(is_page)
        self.source_bytes += size
        self.sources[key] = (size, bool(is_page))

    def observe_source(self, key, size):
        """Check actual reads too, including a loose file growing after collection."""
        previous, is_page = self.sources[key]
        self.check_size(size, is_page)
        extra = max(0, size - previous)
        if self.source_bytes + extra > self.limits.source_bytes:
            raise ExportLimit("实际读取的源文件总大小超过导出限制")
        self.source_bytes += extra
        self.sources[key] = (max(size, previous), is_page)

    def resize_staging(self, key, size):
        self.check()
        previous = self.staging.get(key, 0)
        if size > self.limits.page_bytes or self.staging_bytes - previous + size > self.limits.staging_bytes:
            raise ExportLimit("解包或图片转换的临时文件超过导出限制")
        self.staging[key] = size
        self.staging_bytes += size - previous

    def forget_staging(self, key):
        self.staging_bytes -= self.staging.pop(key, 0)


class BudgetedWriter:
    """Seekable writer enforcing file high-water size before writing, not afterwards.

    No fileno delegation: native encoders must use write() rather than bypassing
    the budget. The caller owns/cleans the underlying file.
    """
    def __init__(self, stream, budget, staging_key=None):
        self.stream, self.budget, self.staging_key = stream, budget, staging_key
        self.size = 0

    def _resize(self, size):
        self.budget.check()
        if self.staging_key is not None:
            self.budget.resize_staging(self.staging_key, size)
        elif size > self.budget.limits.output_bytes:
            raise ExportLimit("最终导出文件超过大小限制")
        self.size = size

    def write(self, data):
        self._resize(max(self.size, self.stream.tell() + len(data)))
        count = self.stream.write(data)
        if count != len(data):
            raise OSError("临时文件写入不完整")
        self.budget.check()
        return count

    def tell(self):
        self.budget.check()
        return self.stream.tell()

    def seek(self, *args):
        self.budget.check()
        return self.stream.seek(*args)

    def truncate(self, size=None):
        size = self.stream.tell() if size is None else size
        self._resize(size)
        return self.stream.truncate(size)

    def flush(self):
        self.budget.check()
        self.stream.flush()
        self.budget.check()

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return True

    @property
    def closed(self):
        return self.stream.closed
