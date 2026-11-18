"""Build and check a new Windows portable release without replacing any instance."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(ROOT))
OPTIONAL_REVIEW = "WINDOWS_VNEXT_REVIEW.md"
SOURCE_PATTERNS = ("app.py", "desktop.py", "launcher.py", "jmcomic_cli.py", "core/*.py", "routes/*.py",
                   "templates/**/*.html", "static/**/*", "requirements*.txt", "build.spec", "tools/*.py",
                   "tools/windows-portable.manifest", "tools/licenses/**/*",
                   "docs/WINDOWS_RELEASE.md", "docs/BACKEND_REVIEW.md", "docs/DATA_MAINTENANCE.md",
                   "docs/AUDIT_FIXES.md", f"docs/{OPTIONAL_REVIEW}",
                   "frontend/src/**/*", "frontend/package*.json", "frontend/tsconfig.json", "frontend/vite.config.js",
                   "tools/Preview-Atelier.cmd", "docs/ATELIER_WINDOWS.md", "LICENSE", "THIRD_PARTY_NOTICES.md")


def source_fingerprint(root=ROOT):
    """Hash release inputs only; databases, logs, downloads and credentials never enter the bundle."""
    root = Path(root)
    files = sorted({path for pattern in SOURCE_PATTERNS for path in root.glob(pattern) if path.is_file()})
    digest = hashlib.sha256()
    for path in files:
        content = path.read_bytes()
        digest.update(f"{path.relative_to(root).as_posix()}\0{len(content)}\0".encode())
        digest.update(content)
    return digest.hexdigest()


def check_bundle(executable, *, cwd, command="self-check"):
    # Prove the frozen program can load its own runtime without a system Python
    # or the build environment on PATH. Self-check overrides all instance paths.
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"} and not key.startswith("JMCONSOLE_")}
    environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    process = subprocess.run([str(executable), command], cwd=cwd, env=environment,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=150 if command == "lifecycle-check" else 90)
    try:
        result = json.loads(process.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError("发行包未返回自检结果；请检查构建依赖与原生DLL") from None
    if process.returncode or result.get("status") != "ok" or result.get("exit_code") != 0:
        raise RuntimeError(f"发行包自检失败：{result.get('error', 'check failed')}")
    return result


def zip_bundle(bundle, output):
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle.parent).as_posix())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("发行ZIP的CRC校验失败")


def release_target(output, source_id):
    requested = Path(output).absolute() if output else ROOT / "releases" / f"windows-x64-{source_id[:12]}"
    if os.path.lexists(requested):
        raise FileExistsError("发行目标已存在，请指定一个新的--output目录")
    target = requested.resolve()
    if target.drive.casefold() != ROOT.resolve().drive.casefold():
        raise ValueError("发行输出须与项目构建目录在同一盘，以便校验后原子发布；可在构建完成后复制ZIP到其他盘")
    return target


def verified_packages():
    """Publish the pinned runtime/build closure; refuse drift in a reused build venv."""
    from core.runtime_dependencies import installed_versions
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pins = {}
    for line in (ROOT / "requirements-windows-build-constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.partition("#")[0].strip()
        if not line:
            continue
        name, expected = line.split("==")
        pins[canonicalize_name(name)] = expected

    # A new top-level or transitive dependency must not silently fall outside
    # the release constraints. Requirements here have no editable installs.
    roots = []
    for file in ("requirements.txt", "requirements-build.txt"):
        for line in (ROOT / file).read_text(encoding="utf-8").splitlines():
            line = line.partition("#")[0].strip()
            if line and not line.startswith(("-r ", "-c ")):
                roots.append(line)
    try:
        packages = dict(installed_versions("\n".join(roots)))
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError(f"缺少发行依赖 {error.name}，请安装 requirements-build.txt") from error
    for name in sorted(packages.keys() | pins.keys()):
        if name not in pins:
            raise RuntimeError(f"{name} 缺少 Windows 发行约束，请先锁定并验证该依赖")
        if name not in packages or packages[name] != pins[name]:
            raise RuntimeError(f"{name}: 发行约束为 {pins[name]}，当前依赖为 {packages.get(name)}；请核对 requirements-build.txt")
    for requirement in map(Requirement, roots):
        if not requirement.marker or requirement.marker.evaluate():
            actual = packages[canonicalize_name(requirement.name)]
            if not requirement.specifier.contains(actual, prereleases=True):
                raise RuntimeError(f"发行版本 {actual} 不符合 {requirement}")
    return packages


def copy_release_docs(root, bundle):
    shutil.copyfile(root / "docs/WINDOWS_RELEASE.md", bundle / "README-Windows.md")
    for name in ("BACKEND_REVIEW.md", "DATA_MAINTENANCE.md", "AUDIT_FIXES.md"):
        shutil.copyfile(root / "docs" / name, bundle / name)
    review = root / "docs" / OPTIONAL_REVIEW
    if review.is_file():
        shutil.copyfile(review, bundle / review.name)
    for item in ('tools/Preview-Atelier.cmd', 'docs/ATELIER_WINDOWS.md'):
        source = root / item
        if source.is_file():
            shutil.copyfile(source, bundle / source.name)


def build(output=None):
    from tools.release_licenses import stage_licenses, verify_bundle_licenses

    if sys.platform != "win32" or platform.machine().upper() not in {"AMD64", "X86_64"}:
        raise RuntimeError("请在64位Windows上构建Windows发行包")
    packages = verified_packages()
    source_id = source_fingerprint()
    target = release_target(output, source_id)
    work = ROOT / "build" / ("windows-" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    manifest = {
        "format": 2, "application": "jmcomic-local-console", "platform": "windows-x64",
        "source_sha256": source_id, "python": platform.python_version(),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "packages": packages,
        "license_sha256": stage_licenses(ROOT, work / "licenses"),
    }
    metadata = work / "release.json"
    metadata.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(work / "dist"),
                    "--workpath", str(work / "cache"), str(ROOT / "build.spec")], cwd=ROOT, check=True,
                   env=dict(os.environ, JMCONSOLE_BUILD_METADATA=str(metadata)))
    if source_fingerprint() != source_id:
        raise RuntimeError("构建期间源代码发生变化，产物未发布；请在代码稳定后重建")
    bundle = work / "dist" / "JMComic"
    verify_bundle_licenses(bundle, manifest["license_sha256"])
    result = check_bundle(bundle / "JMComic.exe", cwd=work)
    lifecycle = check_bundle(bundle / "JMComic.exe", cwd=work, command="lifecycle-check")
    copy_release_docs(ROOT, bundle)
    if source_fingerprint() != source_id:
        raise RuntimeError("自检期间源码发生变化，产物未发布")
    (bundle / "SELF_CHECK.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (bundle / "LIFECYCLE_CHECK.json").write_text(json.dumps(lifecycle, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", bundle / "THIRD_PARTY_NOTICES.md")
    if (ROOT / "LICENSE").is_file():
        shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE")
    publish = work / "publish"
    publish.mkdir()
    # Both locations are known children of this invocation's build directory.
    if not bundle.resolve().is_relative_to(work.resolve()):
        raise RuntimeError("构建产物路径无效")
    os.rename(bundle, publish / "JMComic")
    archive = publish / "JMComic-windows-x64.zip"
    zip_bundle(publish / "JMComic", archive)
    with archive.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    (publish / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    shutil.copyfile(metadata, publish / "release.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target):
        raise FileExistsError("发行目标在构建期间已出现，产物未覆盖")
    os.rename(publish, target)  # Windows refuses replacement of an existing directory.
    if work.resolve().parent == (ROOT / "build").resolve() and not target.is_relative_to(work.resolve()):
        try:
            shutil.rmtree(work)
        except OSError as error:
            print(f"发行已完成，未能清理本次临时目录 {work}: {error}", file=sys.stderr)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="全新的发行输出目录（默认releases/中的源码指纹目录）")
    args = parser.parse_args(argv)
    print(f"Release verified: {build(args.output)}")


if __name__ == "__main__":
    main()
