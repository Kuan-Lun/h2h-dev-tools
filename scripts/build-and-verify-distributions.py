#!/usr/bin/env python3
"""Build an sdist-derived wheel and test it outside the source checkout, offline."""

from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from io import StringIO
from pathlib import Path


def run(*command: str, cwd: Path, environment: Mapping[str, str] | None = None) -> str:
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def single(directory: Path, pattern: str) -> Path:
    found = tuple(directory.glob(pattern))
    if len(found) != 1:
        raise ValueError(f"expected one {pattern} in {directory}, found {len(found)}")
    return found[0]


def provide_dependency(site: Path, name: str) -> None:
    """Copy the complete inventory of one explicitly selected distribution."""
    if name not in {"packaging", "distlib"}:
        raise ValueError(f"unsupported offline dependency: {name}")
    dependency = importlib.metadata.distribution(name)
    record = dependency.read_text("RECORD")
    if not record:
        raise ValueError(f"{name} has no installed-file inventory")
    try:
        rows = list(csv.reader(StringIO(record, newline=""), strict=True))
    except csv.Error as error:
        raise ValueError(f"invalid {name} installed-file inventory: {error}") from error
    metadata = f"{name}-{dependency.version}.dist-info"
    source_root = Path(str(dependency.locate_file(""))).resolve()
    destination_root = site.resolve()
    members: set[Path] = set()
    copies: list[tuple[Path, Path]] = []
    # Distribution.files filters missing entries on Python 3.14. Validate the
    # raw RECORD completely before creating any part of the offline copy.
    for row in rows:
        if len(row) != 3 or not row[0]:
            raise ValueError(f"invalid {name} installed-file inventory row: {row!r}")
        relative = Path(row[0])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.as_posix() != row[0].replace(os.sep, "/")
            or relative.parts[0] not in {name, metadata}
            or relative in members
        ):
            raise ValueError(f"unexpected {name} distribution member: {row[0]!r}")
        members.add(relative)
        source = source_root / relative
        destination = destination_root / relative
        if not source.resolve().is_relative_to(source_root / relative.parts[0]):
            raise ValueError(f"{name} distribution member escapes source: {relative}")
        if not source.is_file():
            raise ValueError(f"missing {name} distribution file: {relative}")
        if not destination.resolve().is_relative_to(
            destination_root / relative.parts[0]
        ):
            raise ValueError(
                f"{name} distribution member escapes destination: {relative}"
            )
        copies.append((source, destination))
    required = {
        Path(name) / "__init__.py",
        Path(metadata) / "METADATA",
        Path(metadata) / "WHEEL",
        Path(metadata) / "RECORD",
    }
    if missing := required - members:
        raise ValueError(
            f"incomplete {name} installed-file inventory: {sorted(missing)}"
        )
    for source, destination in copies:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def provide_runtime_dependencies(site: Path, platform: str) -> None:
    """Supply only the installed distributions needed by this platform's wheel."""
    provide_dependency(site, "packaging")
    if platform == "win32":
        provide_dependency(site, "distlib")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="h2h-dev-tools-distribution-") as temporary:
        scratch = Path(temporary)
        dist = scratch / "dist"
        # build's default flow builds the wheel from the new sdist.
        run(
            sys.executable,
            "-I",
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(dist),
            cwd=root,
        )
        wheel = single(dist, "*.whl")
        single(dist, "*.tar.gz")
        environment = scratch / "smoke"
        run(sys.executable, "-I", "-m", "venv", str(environment), cwd=scratch)
        python = environment / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        site = Path(
            run(
                str(python),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
                cwd=scratch,
            )
        )
        provide_runtime_dependencies(site, sys.platform)
        run(
            str(python),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            str(wheel),
            cwd=scratch,
        )
        probe = """
import importlib.metadata
from pathlib import Path
import sys
import h2h_dev_tools
from packaging.requirements import Requirement
installed = importlib.metadata.distribution('h2h-dev-tools')
assert Path(h2h_dev_tools.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert Path(installed.locate_file('')).resolve().is_relative_to(Path(sys.prefix).resolve())
for text in installed.requires or ():
    requirement = Requirement(text)
    if requirement.marker is None or requirement.marker.evaluate({'extra': ''}):
        assert importlib.metadata.version(requirement.name) in requirement.specifier
assert all(not name.startswith('h2hdb') for name in sys.modules)
print(installed.version)
"""
        version = run(str(python), "-I", "-c", probe, cwd=scratch)
        run(str(python), "-I", "-m", "h2h_dev_tools", "--help", cwd=scratch)
        entrypoint = environment / (
            "Scripts/h2h-dev-tools.exe"
            if sys.platform == "win32"
            else "bin/h2h-dev-tools"
        )
        # Exercise the actual console launcher without inheriting Python's
        # source-path/configuration overrides from the development checkout.
        console_environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("PYTHON")
        }
        console_environment.update(PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1")
        run(
            str(entrypoint),
            "--help",
            cwd=scratch,
            environment=console_environment,
        )
        consumer = scratch / "consumer"
        consumer.mkdir()
        # Exercise every shipped component, including packaged shell resources.
        components = run(
            str(python),
            "-I",
            "-c",
            "import json; from h2h_dev_tools.bundle_verify import COMPONENT_FILES; "
            "print(json.dumps(sorted(COMPONENT_FILES)))",
            cwd=scratch,
        )
        assert isinstance(json.loads(components), list)
        (consumer / "pyproject.toml").write_text(
            "[tool.h2h-dev-tools]\ncomponents = " + components + "\n"
        )
        for command in ("sync", "verify"):
            run(
                str(entrypoint),
                command,
                "--repository",
                str(consumer),
                cwd=scratch,
                environment=console_environment,
            )
        payload_probe = """
from pathlib import Path
import sys
import h2h_dev_tools
from h2h_dev_tools.bundle_verify import BUNDLE_DIRECTORY, verify_bundle
root = Path(sys.argv[1])
package = Path(h2h_dev_tools.__file__).resolve().parent
assert package.is_relative_to(Path(sys.prefix).resolve())
manifest = verify_bundle(root)
for name in manifest['files']:
    assert (root / BUNDLE_DIRECTORY / name).read_bytes() == (package / name).read_bytes(), name
"""
        run(str(python), "-I", "-c", payload_probe, str(consumer), cwd=scratch)
        run(
            str(python),
            "-I",
            "-S",
            str(consumer / "scripts/_shared/h2h_dev_tools/bundle_verify.py"),
            "--repository",
            str(consumer),
            cwd=scratch,
        )
        run(
            str(python),
            "-I",
            "-B",
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "root = Path(sys.argv[1]); sys.path.insert(0, str(root / 'scripts')); "
            "package = root / 'scripts/_shared/h2h_dev_tools'; "
            "runpy.run_path(str(package / 'source_loader.py'))['load_package']("
            "package, '_shared.h2h_dev_tools'); "
            "from _shared.h2h_dev_tools import process; "
            "assert Path(process.__file__).parent == package",
            str(consumer),
            cwd=scratch,
        )
        if arguments.output_directory is not None:
            output = arguments.output_directory.resolve()
            output.mkdir(parents=True, exist_ok=True)
            if any(output.iterdir()):
                raise ValueError(
                    "output directory must be empty to exclude stale artifacts"
                )
            for artifact in dist.iterdir():
                shutil.copyfile(artifact, output / artifact.name)
        print(
            f"Verified sdist-derived h2h-dev-tools {version} wheel in isolated offline environment"
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"distribution-check: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout or "", file=sys.stderr)
            print(error.stderr or "", file=sys.stderr)
        raise SystemExit(1) from error
