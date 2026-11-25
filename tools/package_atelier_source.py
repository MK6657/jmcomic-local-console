"""Package reproducible source inputs, not the checkout or its personal instances."""
from pathlib import Path
from shutil import copyfileobj
from stat import S_IFREG
import sys
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    "app.py", "desktop.py", "launcher.py", "launcher.pyw", "jmcomic_cli.py",
    "start.bat", "start-demo-preview.bat", "build.spec", "pytest.ini", ".gitignore",
    ".github/workflows/tests.yml", ".github/workflows/windows-portable.yml",
    "requirements.txt", "requirements-dev.txt", "requirements-build.txt", "requirements-windows-build-constraints.txt",
    "AGENTS.md", "DESIGN.md", "README.md", "README.zh-CN.md", "THIRD_PARTY_NOTICES.md", "LICENSE",
    "core/*.py", "routes/*.py", "templates/**/*.html",
    "static/**/*.css", "static/**/*.js", "static/**/*.svg", "static/atelier-ui/LICENSE*.txt",
    "static/**/*.woff", "static/**/*.woff2",
    "tools/*.py", "tools/Preview-Atelier.cmd", "tools/windows-portable.manifest",
    "tools/legacy-vue-transfer/*", "tools/legacy-vue-transfer/src/*",
    "tools/licenses/.gitattributes", "tools/licenses/*/LICENSE*",
    "tests/*.py", "tests/README.md", "tests/checks/*.py", "tests/legacy/*.py",
    "tests/js/*.js", "tests/js/*.cjs", "tests/js/*.mjs",
    "docs/README.md", "docs/UPSTREAM_SYNC.md", "docs/ATELIER_WINDOWS.md",
    "docs/WINDOWS_RELEASE.md", "docs/DATA_MAINTENANCE.md", "docs/architecture/*.md", "docs/archive/*.md",
)


def write_source(out):
    root = ROOT.resolve()
    inputs = sorted({file for pattern in PATTERNS for file in root.glob(pattern)
                     if file.is_file()}, key=lambda file: file.relative_to(root).as_posix())
    for file in inputs:
        if file.resolve() != file:
            raise ValueError(f"Unexpected source input: {file.relative_to(root)}")
    with ZipFile(out, "x") as archive:
        for file in inputs:
            entry = ZipInfo((Path("atelier-edition") / file.relative_to(root)).as_posix(),
                            date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (S_IFREG | 0o644) << 16
            entry.compress_type = ZIP_DEFLATED
            entry.file_size = file.stat().st_size
            with file.open("rb") as source, archive.open(entry, "w") as member:
                copyfileobj(source, member)
    with ZipFile(out) as archive:
        if archive.testzip() is not None or len(archive.infolist()) != len(inputs):
            raise RuntimeError("Source ZIP failed integrity verification")
    return len(inputs)


if __name__ == '__main__':
    print(write_source(Path(sys.argv[1])))
