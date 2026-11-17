"""Opt-in Windows PDF memory check using only synthetic files and no upstream."""
import ctypes
from ctypes import wintypes
import gc
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Counters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in
        ("peak", "working", "qpeak", "q", "pp", "p", "pagefile", "pagefilepeak")]


def memory():
    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
    if not get_info(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return counters


def main():
    if sys.platform != "win32":
        raise RuntimeError("This explicit memory probe requires Windows")

    def forbidden(*args, **kwargs):
        raise AssertionError("PDF memory check does not access the network")

    socket.socket.connect = socket.create_connection = forbidden
    from curl_cffi.curl import Curl
    Curl.perform = forbidden
    with tempfile.TemporaryDirectory(prefix="jmcomic-pdf-memory-") as temporary:
        home = Path(temporary)
        for key, child in (("JMCONSOLE_HOME", ""), ("JMCONSOLE_DOWNLOAD_DIR", "downloads"),
                           ("JMCONSOLE_CACHE_DIR", "cache"), ("JMCONSOLE_BACKUP_DIR", "backups")):
            os.environ[key] = str(home / child)
        from routes.api_export import _build_pdf
        from core.logger import shutdown_logging
        from PIL import Image
        import pikepdf
        logging.disable(logging.CRITICAL)
        try:
            images = []
            for number in range(128):
                target = home / f"{number:04d}.png"
                with Image.frombytes("RGB", (512, 512), os.urandom(512 * 512 * 3)) as image:
                    image.save(target)
                images.append(target)
            source_bytes = sum(path.stat().st_size for path in images)
            assert source_bytes > 96 * 1024 ** 2
            gc.collect()
            before = memory()
            output = home / "book.pdf"
            with output.open("w+b") as stream:
                assert _build_pdf(images, stream) == len(images)
            after = memory()
            with pikepdf.Pdf.open(output) as document:
                assert len(document.pages) == len(images)
            growth = max(0, after.peak - max(before.peak, before.working))
            assert growth < 64 * 1024 ** 2, f"PDF peak working set grew {growth} bytes"
            print(json.dumps({"pages": len(images), "source_bytes": source_bytes,
                              "output_bytes": output.stat().st_size, "peak_growth_bytes": growth,
                              "maximum_growth_bytes": 64 * 1024 ** 2, "upstream_requests": 0}))
        finally:
            shutdown_logging()
            logging.shutdown()


if __name__ == "__main__":
    main()
