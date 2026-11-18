"""Package reproducible source inputs, not the checkout or its personal instances."""
from pathlib import Path
import sys
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ('*.py','*.pyw','*.bat','*.md','*.spec','requirements*.txt','pytest.ini','.gitignore',
            'core/*.py','routes/*.py','templates/**/*.html','static/**/*','tools/*.py','tools/*.cmd',
            'tools/*.manifest','tools/licenses/**/*','tests/*.py','tests/*.js','tests/*.cjs','tests/*.mjs',
            'frontend/src/**/*','frontend/package*.json','frontend/vite.config.js','frontend/tsconfig.json','docs/*.md')


def write_source(out):
    inputs = sorted({file for pattern in PATTERNS for file in ROOT.glob(pattern) if file.is_file()})
    with ZipFile(out, 'x', compression=ZIP_DEFLATED) as z:
        for file in inputs:
            rel = file.relative_to(ROOT)
            if file.is_symlink() or not file.resolve().is_relative_to(ROOT) or file.suffix in {'.db','.log','.pyc'}:
                raise ValueError(f'Unexpected source input: {rel}')
            z.write(file, (Path('atelier-edition') / rel).as_posix())
    with ZipFile(out) as z:
        assert z.testzip() is None and len(z.namelist()) == len(inputs)
    return len(inputs)


if __name__ == '__main__':
    print(write_source(Path(sys.argv[1])))
