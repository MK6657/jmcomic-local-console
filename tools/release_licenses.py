"""Stage the license texts from the selected Windows wheels and interpreter."""
import hashlib
from importlib import metadata
from pathlib import Path
import shutil
import sys

from core.runtime_dependencies import installed_versions


NATIVE_CURL_TEXTS = ('LICENSE', 'LICENSE_BORINGSSL', 'LICENSE_BROTLI', 'LICENSE_CARES', 'LICENSE_CURL',
                     'LICENSE_NGHTTP2', 'LICENSE_NGHTTP3', 'LICENSE_NGTCP2', 'LICENSE_ZLIB', 'LICENSE_ZSTD')
PYTHON_NATIVE_TEXTS = tuple(f'LICENSE.{name}.txt' for name in
                            ('bzip2', 'expat', 'libffi', 'liblzma', 'mpdecimal', 'openssl-3', 'sqlite', 'zlib'))
REQUIRED = {'CPython/LICENSE.txt', 'pyinstaller/COPYING.txt', 'setuptools/LICENSE', 'img2pdf/LICENSE',
            'pikepdf/LICENSE.txt', 'pikepdf/third-party-licenses/qpdf.txt', 'pikepdf/third-party-licenses/zlib.txt',
            'pikepdf/third-party-licenses/openssl.txt', 'pikepdf/third-party-licenses/README.md',
            'pillow/LICENSE', 'commonx/LICENSE', 'curl-cffi/LICENSE',
            *('curl-impersonate/' + name for name in NATIVE_CURL_TEXTS),
            *('CPython/' + name for name in PYTHON_NATIVE_TEXTS)}


def _wheel_texts(name):
    distribution = metadata.distribution(name)
    for record in distribution.files or ():
        if '..' in record.parts or record.is_absolute():
            continue
        relative = Path(record)
        if relative.parts[0].endswith('.dist-info'):
            relative = Path(*relative.parts[1:])
            if relative.parts[:1] == ('licenses',):
                relative = Path(*relative.parts[1:])
            elif not relative.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')):
                continue
        elif not relative.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE', 'COPYRIGHT')):
            continue
        yield Path(distribution.locate_file(record)), Path(name) / relative


def stage_licenses(root, destination):
    from curl_cffi import __curl_version__

    runtime = dict(installed_versions((root / 'requirements.txt').read_text(encoding='utf-8')))
    vendor = root / 'tools/licenses'
    inputs = [(Path(sys.base_prefix) / 'LICENSE.txt', Path('CPython/LICENSE.txt'))]
    inputs.extend((vendor / 'python-build-standalone-20260623' / name, Path('CPython') / name)
                  for name in PYTHON_NATIVE_TEXTS)
    for name in sorted(runtime.keys() | {'setuptools', 'pyinstaller'}):
        files = list(_wheel_texts(name))
        if name == 'commonx' and not files:
            files = [(vendor / f'commonx-{runtime[name]}/LICENSE', Path('commonx/LICENSE'))]
        if not files:
            raise RuntimeError(f'{name}: wheel has no license text; review its upstream distribution')
        inputs.extend(files)
    if 'curl-impersonate/2.3.1' not in __curl_version__.split():
        raise RuntimeError('Update the native curl license set for ' + __curl_version__)
    inputs.extend((vendor / 'curl-impersonate-2.3.1' / name, Path('curl-impersonate') / name)
                  for name in NATIVE_CURL_TEXTS)
    destination.mkdir()
    for source, relative in inputs:
        target = destination / relative
        if target.exists() or not source.is_file() or source.stat().st_size == 0:
            raise RuntimeError(f'Missing, empty or duplicate license: {relative}')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    inventory = license_hashes(destination)
    if not REQUIRED <= inventory.keys():
        raise RuntimeError(f'Missing required license texts: {sorted(REQUIRED - inventory.keys())}')
    return inventory


def license_hashes(directory):
    return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob('*')) if path.is_file()}


def verify_bundle_licenses(bundle, expected):
    actual = license_hashes(bundle / '_internal/licenses')
    if actual != expected or not REQUIRED <= actual.keys():
        raise RuntimeError('Portable license files are missing or differ from release.json')
