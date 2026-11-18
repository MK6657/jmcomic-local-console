"""Release notice integrity, including the native libraries inside the Windows wheels."""
from importlib import metadata
from pathlib import Path
import runpy
import shutil
import sys

import pytest

from tools import build_windows, release_licenses


def test_missing_qpdf_notice_is_not_valid_even_with_a_matching_partial_manifest(tmp_path):
    directory = tmp_path / '_internal/licenses'
    for name in release_licenses.REQUIRED - {'pikepdf/third-party-licenses/qpdf.txt'}:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'fixture')
    with pytest.raises(RuntimeError, match='license'):
        release_licenses.verify_bundle_licenses(tmp_path, release_licenses.license_hashes(directory))


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows release wheels')
def test_release_wheels_supply_real_pyinstaller_data_and_detect_changed_notices(tmp_path, monkeypatch):
    pytest.importorskip('PyInstaller')
    from PyInstaller.building.utils import format_binaries_and_datas
    from PyInstaller.utils import hooks

    manifest = release_licenses.stage_licenses(build_windows.ROOT, tmp_path / 'licenses')
    metadata_path = tmp_path / 'release.json'
    metadata_path.write_text('{}')
    monkeypatch.setenv('JMCONSOLE_BUILD_METADATA', str(metadata_path))
    monkeypatch.setattr(hooks, 'collect_submodules', lambda package: [])
    monkeypatch.setattr(hooks, 'collect_data_files', lambda package: [])
    destination = tmp_path / '_internal/licenses'

    class InputsCollected(Exception):
        pass

    def capture_inputs(scripts, *, datas, **kwargs):
        for name, source in format_binaries_and_datas(datas):
            relative = Path(name)
            if relative.parts[0] == 'licenses':
                target = tmp_path / '_internal' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        raise InputsCollected

    with pytest.raises(InputsCollected):
        runpy.run_path(str(build_windows.ROOT / 'build.spec'), init_globals={
            'SPECPATH': str(build_windows.ROOT), 'Analysis': capture_inputs})
    release_licenses.verify_bundle_licenses(tmp_path, manifest)
    qpdf = 'third-party-licenses/qpdf.txt'
    distribution = metadata.distribution('pikepdf')
    record = next(record for record in distribution.files if str(record).endswith('/licenses/' + qpdf))
    embedded = destination / 'pikepdf' / qpdf
    assert embedded.read_bytes() == Path(distribution.locate_file(record)).read_bytes()
    assert b'Apache License' in embedded.read_bytes()
    assert b'Bootloader Exception' in (destination / 'pyinstaller/COPYING.txt').read_bytes()
    embedded.write_bytes(b'replaced')
    with pytest.raises(RuntimeError, match='license'):
        release_licenses.verify_bundle_licenses(tmp_path, manifest)
