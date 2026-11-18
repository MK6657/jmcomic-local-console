# Third-party notices / 第三方声明

The following vendored assets retain their upstream MIT licenses. These licenses
apply to those components, not automatically to this project's original code.
以下许可证适用于对应第三方资源，不自动覆盖本项目原创代码。

- Bootstrap 5.3.3: `static/vendor/bootstrap/`.
  [Upstream license](https://github.com/twbs/bootstrap/blob/v5.3.3/LICENSE).
  Copyright (c) 2011-2024 The Bootstrap Authors.
- Bootstrap Icons 1.11.3: `static/vendor/bootstrap-icons/`, including icon fonts.
  [Upstream license](https://github.com/twbs/icons/blob/v1.11.3/LICENSE).
  Copyright (c) 2019-2024 The Bootstrap Authors.
- Vue 3.5.30: the production runtime in `static/atelier-ui/transfer.js` contains
  Vue, @vue/runtime-core, @vue/runtime-dom, @vue/reactivity, and @vue/shared.
  The original MIT license for each package is included alongside it in `LICENSE-*.txt`.
  Vite and TypeScript are used at build time only; they are not required to run the program.

Both use the following permission and warranty terms:

The MIT License (MIT)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

## Windows portable distribution / Windows 便携包

Source installs resolve `requirements.txt` in the user's Python environment. The portable EXE redistributes CPython, a PyInstaller bootloader, Python modules, native libraries and the local frontend assets. Keep this file, `_internal/licenses/` and `_internal/release.json` with the EXE. Format 2 of the release manifest records the installed versions and SHA-256 of each collected license text; build-only packages listed in `packages` are not automatically part of the executable.

源码版单独安装 Python 依赖；便携包已经包含运行时。以下路径相对于包内 `_internal/licenses/`，对应组件保留各自的许可，不授予本项目原创代码许可。旧包的文件清单不会因本说明更新而改变。

| Component / upstream source | Terms and location in the portable bundle |
| --- | --- |
| [CPython](https://www.python.org/downloads/source/) and [PyInstaller](https://github.com/pyinstaller/pyinstaller/releases/tag/v6.22.3) bootloader | `CPython/LICENSE.txt` from the actual interpreter; the additional `CPython/LICENSE.*.txt` are from [python-build-standalone 20260623](https://github.com/astral-sh/python-build-standalone/tree/20260623), the CPython 3.11.15 toolchain used to inspect the Windows runtime. They cover bzip2, Expat, libffi, liblzma, mpdecimal, OpenSSL 3, SQLite and zlib. `pyinstaller/COPYING.txt` contains the GPL terms and bootloader exception. |
| Microsoft Visual C++ / Universal CRT | The Windows DLLs retain Microsoft's terms. See `pikepdf/third-party-licenses/microsoft-visual-cpp-runtime.txt` and the [Microsoft redistributable list](https://aka.ms/vs/17/redistribution). |
| [img2pdf 0.6.0](https://pypi.org/project/img2pdf/0.6.0/#files) | LGPL-3.0-or-later. `img2pdf/LICENSE`; the GPLv3 text it incorporates is also available at `pikepdf/third-party-licenses/GPL-3.0.txt`. |
| [pikepdf 10.18.2](https://github.com/pikepdf/pikepdf/tree/v10.18.2) / [qpdf 12.4.2](https://github.com/qpdf/qpdf/tree/v12.4.2) | pikepdf: MPL-2.0 (`pikepdf/LICENSE.txt`). Its Windows wheel ships a separate qpdf DLL: Apache-2.0 (`pikepdf/third-party-licenses/qpdf.txt`), including statically linked OpenSSL and zlib. That directory contains the wheel's full third-party notices and platform mapping in `README.md`. |
| [Pillow](https://python-pillow.org/), [lxml](https://lxml.de/) | `pillow/LICENSE` includes the imaging codec notices; `lxml/LICENSE.txt` and `lxml/LICENSES.txt` include lxml and XML library terms. |
| [curl_cffi 0.17.0](https://github.com/lexiforest/curl_cffi/tree/v0.17.0) / [curl-impersonate 2.3.1](https://github.com/lexiforest/curl-impersonate/releases/tag/v2.3.1) | `curl-cffi/LICENSE` (MIT) covers the binding. `curl-impersonate/` includes the Windows native distribution's own LICENSE and texts for curl, BoringSSL, Brotli, c-ares, nghttp2/3, ngtcp2, zlib and zstd. |
| [jmcomic](https://github.com/hect0x7/JMComic-Crawler-Python) / [commonX](https://github.com/hect0x7/common/tree/v0.6.41) | MIT, `jmcomic/LICENSE` and `commonx/LICENSE`. The latter comes from the matching upstream tag because the wheel contains no license file. |
| Other frozen modules | Per-distribution directories include Flask and its dependencies, Waitress, packaging (Apache/BSD alternatives), requests/urllib3, certifi's CA bundle, PyCryptodome and setuptools with vendored notices. See the license files for the applicable copyright and terms. |

The build reads the installed distributions, not the developer's entire site-packages. For texts missing from the wheels, `tools/licenses/commonx-0.6.41/LICENSE` is from the tag above (SHA-256 `933e1ca13c59c6e1b18ac2b75b4d2d8cae7b661dd170c1aaf849db5eb2bbfb10`); the ten `tools/licenses/curl-impersonate-2.3.1/LICENSE*` files are verbatim members of upstream `libcurl-impersonate-v2.3.1.x86_64-win32.tar.gz` (asset SHA-256 `22753b8eaeb2d69fa4272e0b8282106e6d411195e6515399a21767c26750a64c`). No network license lookup is needed to package or run the program.
