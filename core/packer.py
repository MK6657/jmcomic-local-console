"""
CBZ / 漫画打包模块
"""
import os
import zipfile
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from xml.etree import ElementTree as ET

from . import archive_pages
from .logger import log
from .validation import EXPORT_IMAGE_EXTENSIONS
from .file_tree import safe_files

_publish_lock = threading.Lock()


class Packer(ABC):
    """打包器抽象基类"""

    @abstractmethod
    def extension(self) -> str:
        """返回文件扩展名，如 .cbz"""

    @abstractmethod
    def pack(self, source_dir: Path, output_path: Path, on_progress=None) -> Path:
        """
        将 source_dir 中的所有图片打包为指定格式。

        Args:
            source_dir: 源目录，包含图片文件
            output_path: 输出目标路径（不含扩展名或含最终扩展名）
            on_progress: 可选回调 on_progress(current, total)

        Returns:
            最终生成的压缩包完整路径
        """


class CbzPacker(Packer):
    """CBZ (ZIP) 格式漫画打包器"""

    def extension(self) -> str:
        return ".cbz"

    @staticmethod
    def packable_images(source_dir) -> list[Path]:
        """会被打包的散图：图片后缀、非空、不超过阅读器的单页上限（其他的留在原处，也不会被当作已打包的原图删掉）"""
        images = []
        for entry in safe_files(Path(source_dir)):
            if entry.is_file() and entry.suffix.lower() in EXPORT_IMAGE_EXTENSIONS:
                size = entry.stat().st_size
                if 0 < size <= archive_pages.MAX_PAGE_BYTES:
                    images.append(entry)
                else:
                    log.warning(f"打包跳过空的或过大的图片: {entry} ({size} 字节)")
        return images

    def pack(self, source_dir: Path, output_path: Path, on_progress=None, base=None) -> Path:
        """
        将 source_dir 中的所有图片打包为 .cbz 文件。

        - base：目录里已有的压缩包（core.archive_pages 的页目录，状态 ok 或 empty），一个或按优先顺序的一组。
          它们里面散图没有的页一起放进新包（同一页用散图，其次用排在前面的压缩包，archive_pages.page_key）——
          打包后删了原图、这次只下载了新章节或只重下了一部分时，旧的页不会因为新包覆盖旧包而丢失。
          旧包有被跳过的页（带不过来）、或读不出旧包的页时整个打包失败，旧包原样保留。
        - 打包的页数记在 self.packed_count（散图 + 带过来的页），调用方可据此核对新包。
        - 流式写入磁盘 .cbz.tmp 文件
        - 使用 ZIP_DEFLATED 压缩
        - 如果 on_progress 可调用，每处理一张图调用 on_progress(current, total)
        - 完成后 os.rename(.cbz.tmp → .cbz)
        - try/finally 确保异常时清理 .cbz.tmp
        - 同时生成 ComicInfo.xml 包含 Title/Series/Writer
        """
        source_dir = Path(source_dir)
        if not source_dir.exists() or not source_dir.is_dir():
            raise FileNotFoundError(f"源目录不存在: {source_dir}")

        # 收集图片文件
        image_files = self.packable_images(source_dir)

        bases = [] if base is None else (list(base) if isinstance(base, (list, tuple)) else [base])
        keep = []   # [(旧包, 页)]
        have = {archive_pages.page_key(path.relative_to(source_dir).as_posix()) for path in image_files}
        for index in bases:
            if index.status not in ("ok", "empty") or index.skipped:
                raise ValueError(f"旧压缩包 {index.path.name} 有读不了的页，不能带进新包（{index.status}）")
            # 只被散图和排在前面的旧包挡住；同一个旧包里的页互不影响（与阅读器看到的一致）
            mine = set()
            for page in index.pages:
                key = archive_pages.page_key(page.name)
                if key not in have:
                    mine.add(key)
                    keep.append((index, page))
            have |= mine

        if not image_files and not keep:
            raise ValueError(f"源目录中未找到任何图片: {source_dir}")

        total = len(image_files) + len(keep)
        self.packed_count = total
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=output_path.name + ".", suffix=".tmp", dir=output_path.parent)
        os.close(fd)
        tmp_path = Path(name)

        try:
            with zipfile.ZipFile(
                str(tmp_path), "w", zipfile.ZIP_DEFLATED
            ) as zf:
                # 写入 ComicInfo.xml
                comic_info_xml = self._build_comic_info(
                    title=source_dir.name,
                    series=source_dir.name,
                    writer="",
                    page_count=total,
                )
                zf.writestr("ComicInfo.xml", comic_info_xml)

                # 写入所有图片
                for idx, img_path in enumerate(image_files, start=1):
                    arcname = str(img_path.relative_to(source_dir))
                    # 使用 / 作为路径分隔符（ZIP 标准）
                    arcname = arcname.replace("\\", "/")
                    zf.write(str(img_path), arcname)
                    if on_progress and callable(on_progress):
                        on_progress(idx, total)

                # 旧包里散图没有的页（校验 CRC 与大小后原样放进新包；每个旧包只打开一次）
                idx = len(image_files)
                for index in bases:
                    pages = [page for owner, page in keep if owner is index]
                    if not pages:
                        continue
                    with archive_pages.open_pages(index) as read:
                        for page in pages:
                            zf.writestr(page.name, read(page))
                            idx += 1
                            if on_progress and callable(on_progress):
                                on_progress(idx, total)

            # 重命名 .tmp → .cbz（原子操作）
            with _publish_lock:
                os.replace(str(tmp_path), str(output_path))
            log.info(
                f"CBZ 打包完成: {output_path} ({total} 张图片)"
            )
            return output_path

        except Exception:
            # 清理临时文件
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def _build_comic_info(
        self, title: str = "", series: str = "", writer: str = "", page_count: int = 0
    ) -> str:
        """生成 ComicInfo.xml 元数据"""
        root = ET.Element("ComicInfo")
        root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
        root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")

        ET.SubElement(root, "Title").text = title
        ET.SubElement(root, "Series").text = series
        ET.SubElement(root, "Writer").text = writer
        ET.SubElement(root, "PageCount").text = str(page_count)

        return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode(
            "utf-8"
        )
