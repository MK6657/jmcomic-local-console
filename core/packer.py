"""
CBZ / 漫画打包模块
"""
import os
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from xml.etree import ElementTree as ET

from .logger import log
from .validation import EXPORT_IMAGE_EXTENSIONS


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

    def pack(self, source_dir: Path, output_path: Path, on_progress=None) -> Path:
        """
        将 source_dir 中的所有图片打包为 .cbz 文件。

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
        image_files: list[Path] = []
        for entry in sorted(source_dir.rglob("*")):
            if entry.is_file() and entry.suffix.lower() in EXPORT_IMAGE_EXTENSIONS:
                image_files.append(entry)

        if not image_files:
            raise ValueError(f"源目录中未找到任何图片: {source_dir}")

        total = len(image_files)
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

        # 清理上次残留的临时文件（如进程崩溃遗留）
        if tmp_path.exists():
            try:
                tmp_path.unlink()
                log.info(f"清理残留临时文件: {tmp_path}")
            except OSError as e:
                log.warning(f"无法清理残留临时文件: {tmp_path} error={e}")

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

            # 重命名 .tmp → .cbz（原子操作）
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
