"""Windowless Windows entry point; shares the tested console launcher."""
import ctypes

if __name__ == "__main__":
    try:
        from launcher import main
        main([], quiet=True)
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(
            None, f"{exc}\n\n请先运行 start.bat 安装依赖，或查看 runtime\\logs\\launcher.log。", "JMComic 启动失败", 0x10,
        )
