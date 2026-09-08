# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置文件 — JMComic 下载控制台
"""
import sys
from pathlib import Path

# 项目根目录
ROOT = Path(SPECPATH).resolve()

block_cipher = None

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 模板文件
        (str(ROOT / "templates"), "templates"),
        # 静态文件（CSS/JS/字体等）
        (str(ROOT / "static"), "static"),
    ],
    hiddenimports=[
        "core",
        "core.path_utils",
        "core.logger",
        "core.database",
        "core.job_manager",
        "core.jm_service",
        "core.path_guard",
        "core.progress",
        "core.settings",
        "core.packer",
        "core.scheduler",
        "core.validation",
        "routes",
        "routes.page_routes",
        "routes.api_search",
        "routes.api_album",
        "routes.api_jobs",
        "routes.api_settings",
        "routes.api_preview",
        "routes.api_export",
        "routes.api_wishlist",
        "routes.api_library",
        "routes.api_system",
        # 第三方动态导入（在 try/except 中，需要显式指定）
        "img2pdf",
        "waitress",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "tcl",
        "idlelib",
        "unittest",
        "distutils",
        "pdb",
        "xmlrpc",
        "venv",
        "matplotlib",
        "zmq",
        "jedi",
        "parso",
        "scipy",
        "numpy",
        "pandas",
        "pynput",
        "watchdog",
        "notebook",
        "jupyter",
        "nbdime",
        "nbconvert",
        "nbformat",
        "ipython",
        "ipykernel",
        "setuptools",
        "pip",
        "wheel",
        # jmcomic 缓存相关排除
        "jmcomic.cache",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="app",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 显示控制台窗口，方便调试
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    contents_directory=".",
)
