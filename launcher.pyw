"""Windowless Windows entry point; shares the tested console launcher."""
import ctypes
from launcher import main

if __name__ == "__main__":
    try:
        main([], quiet=True)
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(
            None, f"{exc}\n\n请先运行 start.bat 安装依赖。", "JMComic 启动失败", 0x10,
        )
