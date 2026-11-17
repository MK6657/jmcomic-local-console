# -*- mode: python ; coding: utf-8 -*-
"""Windows one-folder bundle. Run tools/build_windows.py for verified output."""
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve()
metadata = Path(os.environ["JMCONSOLE_BUILD_METADATA"])
datas = [(str(ROOT / "templates"), "templates"), (str(ROOT / "static"), "static"),
         (str(metadata), ".")]
# These SDK packages discover implementations dynamically. The remaining native
# packages (curl_cffi, Pillow, pikepdf) use PyInstaller's standard/contrib hooks.
for package in ("jmcomic", "commonx"):
    datas += collect_data_files(package)
for distribution in ("jmcomic", "commonx", "curl_cffi", "Pillow", "img2pdf", "Flask", "requests", "waitress"):
    datas += copy_metadata(distribution)

a = Analysis(
    [str(ROOT / "desktop.py")], pathex=[str(ROOT)], binaries=[], datas=datas,
    hiddenimports=collect_submodules("jmcomic") + collect_submodules("commonx")
                  + ["PIL.WebPImagePlugin", "PIL.GifImagePlugin"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "PyInstaller", "tkinter", "matplotlib", "numpy", "pandas"],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="JMComic",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, contents_directory="_internal", manifest=str(ROOT / "tools" / "windows-portable.manifest"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="JMComic")
