"""A new SDK dependency cannot silently enter the portable runtime unpinned."""
import ast
from importlib import metadata
from pathlib import Path
import sys
from types import SimpleNamespace

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pytest

from tools import build_windows


def test_direct_imports_are_declared_in_source_runtime_requirements():
    root = Path(__file__).resolve().parents[1]
    owners = metadata.packages_distributions()
    declared = {canonicalize_name(Requirement(line).name)
                for raw in (root / 'requirements.txt').read_text(encoding='utf-8').splitlines()
                if (line := raw.partition('#')[0].strip())}
    missing = set()
    for pattern in ('*.py', 'core/*.py', 'routes/*.py'):
        for source in root.glob(pattern):
            for node in ast.walk(ast.parse(source.read_bytes(), filename=str(source))):
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [node.module]
                elif isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                else:
                    continue
                for module in modules:
                    top = module.split('.')[0]
                    if top in sys.stdlib_module_names or (root / (top + '.py')).exists() or (root / top / '__init__.py').exists():
                        continue
                    providers = owners.get(top, [])
                    if providers and declared.isdisjoint(map(canonicalize_name, providers)):
                        missing.add((source.name, module, tuple(providers)))
    assert not missing, sorted(missing)


def test_release_refuses_unpinned_or_drifted_transitive_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(build_windows, "ROOT", tmp_path)
    (tmp_path / "requirements-build.txt").write_text("-r requirements.txt\n")
    (tmp_path / "requirements.txt").write_text("jmcomic==2.7.10\n")
    pins = tmp_path / "requirements-windows-build-constraints.txt"
    pins.write_text("jmcomic==2.7.10\n")
    installed = {"jmcomic": "2.7.10", "curl-cffi": "0.17.0"}
    monkeypatch.setattr(build_windows.importlib.metadata, "distribution", lambda name: SimpleNamespace(
        version=installed[name], requires=["curl_cffi>=0.17.0", 'test-only; extra == "testing"'] if name == "jmcomic" else []))
    with pytest.raises(RuntimeError, match="curl-cffi.*缺少"):
        build_windows.verified_packages()
    pins.write_text("jmcomic==2.7.10\ncurl-cffi==0.17.0\n")
    assert build_windows.verified_packages() == installed
    installed["curl-cffi"] = "0.18.0"
    with pytest.raises(RuntimeError, match="curl-cffi.*0.18.0"):
        build_windows.verified_packages()
