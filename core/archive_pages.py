"""压缩包（CBZ / 本程序打包的 ZIP）里的漫画页：阅读器、“本地可读”判断、导出和自动打包共用同一套安全规则。

自动打包（core.jm_service + core.packer.CbzPacker）在漫画目录里写 <目录名>.cbz 或 <目录名>.zip：
页面按章节相对路径存放（如“第1话/001.jpg”），另有 ComicInfo.xml；打包后可以删掉散图。只剩压缩包时，
阅读器和导出直接从压缩包里读页，从不把文件解到下载目录里。

用哪个压缩包（select）：漫画目录第一层里本程序写的 <目录名>.cbz / <目录名>.zip（两种格式都在时新的优先——
切换过打包格式后重新下载，新写的那个才完整），然后是第一层里别的 .cbz（按名称自然排序）；
第一个能读的就用它，都读不了时按第一个说明原因。别的 .zip 不认（可能是别的东西）。不跟随符号链接 / 目录联接。

哪些条目算页：路径安全（不是绝对路径、没有盘符、“..”、空字节，不太长；Windows 工具写的反斜杠按“/”理解），
后缀是图片（EXPORT_IMAGE_EXTENSIONS；阅读器只显示 ALLOWED_EXTENSIONS 那几种），没有加密，
压缩方式是“不压缩”或 DEFLATE（本程序和常见 CBZ 工具都用这两种；只有它们解压时 zipfile 能按声明的大小截住，
BZIP2 / LZMA 会把整段一次解开，几 KB 的条目就能吃掉几 GB 内存），解压后大小在限制内。
ComicInfo.xml 等其他文件不算页，__MACOSX/ 和 “._” 开头的 macOS 附带文件忽略（以“.”开头的章节目录照常算：
章节标题本来就可能以“.”开头）。按路径自然排序（与散图同一规则：core.file_tree.natural_key），
同名条目只取通过检查的最后一个，读页时打开的正是检查过的那个条目（不按名字重新查找）。

压缩包的状态（read_index）：
  ok       至少有一页阅读器能显示的图片
  empty    能打开，但没有一页阅读器能显示的图片
  corrupt  打不开（目录损坏、不是 ZIP、条目多到不合理），或者第一页读出来校验不对
条目目录按压缩包的（大小, 修改时间）记住（最近用过的 _CACHE_MAX 个），压缩包一变就重新读；另记每个压缩包的状态
（轻量，_STATUS_MAX 个），“本地可读”判断整个资源库时不必重新打开没变的压缩包。读页时逐页校验 CRC 与大小，
压缩包在建目录之后变了就报错（ArchiveError），不会读到别的内容。读不出来的任何错误都算“损坏”，不会变成 500；
打开文件本身出错（被占用、没有权限）只是暂时的（transient）：不记住，也不据此做不可逆的决定（取代旧任务、覆盖旧包）。
skipped 是被跳过的图片条目数（不支持的压缩方式、加密、太大、路径不安全）：自动打包时有这种条目就不覆盖旧包。

散图与压缩包都在时（merge_pages）：同一章节同一页（相同的相对路径，不看扩展名）用散图，压缩包只补散图没有的页，
一页不会出现两次——自动打包后没删原图时两边相同，只显示散图；重新下载到一半、或只下载了新章节时，
压缩包里其余的页仍然看得到。
"""
import os
import posixpath
import stat
import threading
import zipfile
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .file_tree import _REPARSE_POINT, natural_key
from .validation import EXPORT_IMAGE_EXTENSIONS, is_allowed_image

ARCHIVE_SUFFIXES = (".cbz", ".zip")
MAX_ENTRIES = 200000                       # 条目数上限（很长的连载几百话 × 几十页也远小于此；自动打包不会超过）
MAX_PAGE_BYTES = 64 * 1024 * 1024          # 一页解压后最大
MAX_TOTAL_BYTES = 256 * 1024 ** 3          # 整个压缩包里的图片解压后合计最大（自动打包不会超过）
MAX_NAME = 1024
_SUPPORTED_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}

_CACHE_MAX = 256                 # 完整的页目录（含每页的条目信息）只记最近用过的这么多个
_STATUS_MAX = 20000              # 只记状态（很小）：整个资源库的“本地可读”判断用
_cache: "OrderedDict[str, tuple]" = OrderedDict()   # 规范化路径 → (签名, ArchiveIndex)
_status: "OrderedDict[str, tuple]" = OrderedDict()  # 规范化路径 → (签名, 状态, 格式)
_cache_lock = threading.Lock()


class ArchiveError(Exception):
    """压缩包或其中一页读不出来"""


class ArchiveBusy(ArchiveError):
    """压缩包暂时打不开（被别的程序占用、没有权限、内存不足）：不是损坏，稍后再试"""


def _is_transient(error: BaseException) -> bool:
    """暂时的读文件错误（被占用、没有权限、刚被移走、内存不足）；其他 OSError（如偏移量不对导致的
    “Invalid argument”）是压缩包本身坏了"""
    if isinstance(error, (MemoryError, PermissionError, FileNotFoundError)):
        return True
    return isinstance(error, OSError) and getattr(error, "winerror", None) in (32, 33)  # 共享 / 锁定冲突


@dataclass(frozen=True)
class ArchivePage:
    member: str     # 压缩包里的原始条目名
    name: str       # 规范化后的路径（“/”分隔，导出时用作文件名）
    chapter: str    # 所在目录（直接放在第一层时为空）
    size: int       # 解压后大小
    info: zipfile.ZipInfo = field(default=None, compare=False, repr=False)  # 通过检查的那个条目（读页时直接打开它）

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.name).suffix.lower()

    @property
    def readable(self) -> bool:
        """阅读器能显示（与散图同一套允许的后缀）"""
        return is_allowed_image(self.suffix)


@dataclass(frozen=True)
class ArchiveIndex:
    path: Path
    status: str                 # 'ok' | 'empty' | 'corrupt'
    pages: tuple = ()           # 所有图片页（导出用，含 bmp / avif）
    reason: str = ""            # 打不开的原因（日志用）
    signature: tuple | None = None  # 建目录时压缩包的（大小, 修改时间）
    skipped: int = 0            # 被跳过的图片条目数（见模块说明）
    transient: bool = False     # 打开文件本身出错：可能只是暂时的

    @property
    def format(self) -> str:
        return self.path.suffix.lower().lstrip(".")

    @property
    def reader_pages(self) -> tuple:
        """阅读器显示的页"""
        return tuple(page for page in self.pages if page.readable)

    def find(self, name: str):
        """按页名找阅读器页：(序号从 1 起, 页)；名字对不上时按 page_key（同一页换了格式）；找不到 → None"""
        pages = self.reader_pages
        for number, page in enumerate(pages, start=1):
            if page.name == name:
                return number, page
        key = page_key(name)
        for number, page in enumerate(pages, start=1):
            if page_key(page.name) == key:
                return number, page
        return None


def page_key(name: str) -> str:
    """同一页的判断依据：相对路径去掉扩展名、不分大小写（散图重新下载后格式可能变了，如 .jpg → .webp）"""
    return posixpath.splitext(name.replace("\\", "/"))[0].casefold()


def _is_plain_file(entry) -> bool:
    try:
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            return False
        if os.name == "nt" and entry.stat(follow_symlinks=False).st_file_attributes & _REPARSE_POINT:
            return False
        return True
    except OSError:
        return False


def candidates(folder) -> list[Path]:
    """漫画目录第一层里可以用的压缩包，按优先顺序（规则见模块说明）。folder 须已通过路径安全检查。"""
    folder = Path(folder)
    found = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if os.path.splitext(entry.name)[1].lower() in ARCHIVE_SUFFIXES and _is_plain_file(entry):
                    found[entry.name.lower()] = entry.name
    except (OSError, ValueError):
        return []
    own = [folder / found[name.lower()] for name in (folder.name + ".cbz", folder.name + ".zip")
           if name.lower() in found]
    own.sort(key=lambda path: (_signature(path) or (0, 0))[1], reverse=True)  # 新写的优先
    own_names = {path.name.lower() for path in own}
    others = sorted((name for key, name in found.items() if key.endswith(".cbz") and key not in own_names),
                    key=natural_key)
    return own + [folder / name for name in others]


def find_archive(folder) -> Path | None:
    """优先用的那个压缩包（不检查能不能读）；没有则 None。"""
    found = candidates(folder)
    return found[0] if found else None


def select(folder) -> ArchiveIndex | None:
    """该用的压缩包：按优先顺序第一个能读的；都读不了时优先报暂时打不开的那个（不说成损坏），
    否则第一个（说明原因用）；一个都没有 → None。"""
    first = busy = None
    for path in candidates(folder):
        index = read_index(path)
        if index.status == "ok":
            return index
        first = first or index
        busy = busy or (index if index.transient else None)
    return busy or first


def select_status(folder):
    """只要状态时的 select：(压缩包, 状态, 格式, 是否暂时的)；没有压缩包 → None。
    压缩包没变时用记住的状态，不打开压缩包、不留完整页目录（判断整个资源库时用）。
    没有能读的时，只要有一个候选是暂时打不开，就报“暂时的”：据此不能断定本地已经没有能读的页。"""
    first, busy = None, False
    for path in candidates(folder):
        found = quick_status(path)
        if found[0] == "ok":
            return (path,) + found
        busy = busy or found[2]
        first = first or ((path,) + found)
    return None if first is None else first[:3] + (busy,)


def quick_status(path) -> tuple:
    """(状态, 格式, 是否暂时的)：压缩包没变时直接用记住的状态"""
    path = Path(path)
    signature = _signature(path)
    if signature is None:
        return "corrupt", path.suffix.lower().lstrip("."), False
    key = os.path.normcase(os.path.abspath(path))
    with _cache_lock:
        cached = _status.get(key)
        if cached and cached[0] == signature:
            _status.move_to_end(key)
            return cached[1], cached[2], False
    index = read_index(path)
    return index.status, index.format, index.transient


def _page_name(info: zipfile.ZipInfo) -> str | None:
    """条目算一页时返回规范化后的路径，否则 None（目录、不安全的路径、非图片、加密、不支持的压缩方式、太大）"""
    name = info.filename.replace("\\", "/")
    if not name or len(name) > MAX_NAME or "\x00" in name or name.endswith("/"):
        return None
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if parts[0] == "__MACOSX" or parts[-1].startswith("._"):
        return None
    if PurePosixPath(name).suffix.lower() not in EXPORT_IMAGE_EXTENSIONS:
        return None
    if info.flag_bits & 0x1 or info.compress_type not in _SUPPORTED_METHODS:
        return None
    if info.file_size <= 0 or info.file_size > MAX_PAGE_BYTES:
        return None
    return name


def _read_member(zf: zipfile.ZipFile, page: ArchivePage) -> bytes:
    with zf.open(page.info or page.member) as handle:
        data = handle.read(MAX_PAGE_BYTES + 1)  # zipfile 读到末尾时校验 CRC，不对就抛 BadZipFile
    if len(data) != page.size:
        raise ArchiveError(f"页大小不符: {page.name}")
    return data


def _is_image_entry(info: zipfile.ZipInfo) -> bool:
    """看起来是一页图片的条目（不含 macOS 附带文件）：用来数被跳过的页"""
    name = info.filename.replace("\\", "/")
    parts = name.split("/")
    if name.endswith("/") or parts[0] == "__MACOSX" or parts[-1].startswith("._"):
        return False
    return PurePosixPath(name).suffix.lower() in EXPORT_IMAGE_EXTENSIONS


def _build_index(path: Path, signature) -> tuple[ArchiveIndex, bool]:
    """(页目录, 能不能记住)。读文件本身出错（被占用、没有权限、内存不足）时不记住，下次再试。"""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if any(info.header_offset < 0 for info in infos):   # 文件头部被截掉：偏移量指到文件前面
                return ArchiveIndex(path, "corrupt", reason="条目位置不对", signature=signature), True
            if len(infos) > MAX_ENTRIES:
                return ArchiveIndex(path, "corrupt", reason=f"条目太多（{len(infos)}）", signature=signature), True
            found: dict[str, ArchivePage] = {}
            skipped = 0
            for info in infos:
                name = _page_name(info)
                if name is None:
                    skipped += _is_image_entry(info) and info.file_size > 0  # 0 字节的“图片”不是页（中断的下载留下的）
                    continue
                chapter = name.rsplit("/", 1)[0] if "/" in name else ""
                if name in found:
                    skipped += 1  # 同名条目：只用最后一个
                found[name] = ArchivePage(member=info.filename, name=name, chapter=chapter,
                                          size=info.file_size, info=info)
            if sum(page.size for page in found.values()) > MAX_TOTAL_BYTES:
                return ArchiveIndex(path, "corrupt", reason="解压后太大", signature=signature), True
            pages = tuple(sorted(found.values(), key=lambda page: natural_key(page.name)))
            index = ArchiveIndex(path, "ok", pages, signature=signature, skipped=skipped)
            first = next(iter(index.reader_pages), None)
            if first is None:
                return ArchiveIndex(path, "empty", pages, signature=signature, skipped=skipped), True
            _read_member(zf, first)  # 读一页：截断、数据损坏多半在这里就能发现
            return index, True
    except zipfile.BadZipFile as e:
        return ArchiveIndex(path, "corrupt", reason=str(e) or "BadZipFile", signature=signature), True
    except (OSError, MemoryError) as e:
        transient = _is_transient(e)
        return ArchiveIndex(path, "corrupt", reason=str(e) or e.__class__.__name__, signature=signature,
                            transient=transient), not transient
    except Exception as e:  # 任何读不出来的情况都算损坏（如解压错误），不让一个压缩包拖垮整个列表
        return ArchiveIndex(path, "corrupt", reason=f"{e.__class__.__name__}: {e}", signature=signature), True


def _signature(path: Path):
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        return None
    return (info.st_size, info.st_mtime_ns)


def read_index(path) -> ArchiveIndex:
    """压缩包的页目录与状态；压缩包没变时直接用记住的。已不在 / 不是普通文件 → corrupt（调用方先用 select 找）。"""
    path = Path(path)
    signature = _signature(path)
    if signature is None:
        return ArchiveIndex(path, "corrupt", reason="文件不在或不是普通文件")
    key = os.path.normcase(os.path.abspath(path))
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] == signature:
            _cache.move_to_end(key)
            return cached[1]
    index, cacheable = _build_index(path, signature)
    if cacheable:
        with _cache_lock:
            _cache[key] = (signature, index)
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)   # 最久没用的
            _status[key] = (signature, index.status, index.format)
            _status.move_to_end(key)
            while len(_status) > _STATUS_MAX:
                _status.popitem(last=False)
    return index


def version(index: ArchiveIndex) -> str:
    """页地址里的版本号：压缩包换了（大小或修改时间变了）地址就变，浏览器不会拿到旧页"""
    signature = index.signature or (0, 0)
    return f"{signature[0]:x}-{signature[1]:x}"


@contextmanager
def open_pages(index: ArchiveIndex):
    """打开压缩包一次，逐页读：with open_pages(index) as read: data = read(page)。
    压缩包在建目录之后变了、打不开、某页读不出来 → ArchiveError。"""
    if index.signature is None or _signature(index.path) != index.signature:
        raise ArchiveError("压缩包已经变了，请重新打开")
    try:
        zf = zipfile.ZipFile(index.path)
    except Exception as e:
        raise (ArchiveBusy if _is_transient(e) else ArchiveError)(str(e) or e.__class__.__name__) from e

    def read(page: ArchivePage) -> bytes:
        try:
            return _read_member(zf, page)
        except ArchiveError:
            raise
        except Exception as e:  # 被占用等是暂时的；校验不对、偏移量不对等是坏了
            kind = ArchiveBusy if _is_transient(e) else ArchiveError
            raise kind(f"{page.name}: {e.__class__.__name__}: {e}") from e

    try:
        yield read
    finally:
        zf.close()


def read_page(index: ArchiveIndex, page: ArchivePage) -> bytes:
    """读出一页的内容（校验 CRC 与大小）；读不出来抛 ArchiveError"""
    with open_pages(index) as read:
        return read(page)


def merge_pages(loose, index: ArchiveIndex | None, reader_only: bool = True) -> list:
    """散图与压缩包里的页合在一起：loose 是 [(相对路径, 文件)]（显示顺序），返回 [(相对路径, 文件或 ArchivePage)]。
    同一页（page_key 相同）用散图；压缩包不可用或没有要补的页时原样返回 loose，否则按相对路径自然排序。"""
    loose = list(loose)
    if index is None or index.status != "ok":
        return loose
    have = {page_key(name) for name, _ in loose}
    extra = [(page.name, page) for page in (index.reader_pages if reader_only else index.pages)
             if page_key(page.name) not in have]
    if not extra:
        return loose
    return sorted(loose + extra, key=lambda item: natural_key(item[0].replace("\\", "/")))


def clear_cache():
    with _cache_lock:
        _cache.clear()
        _status.clear()
