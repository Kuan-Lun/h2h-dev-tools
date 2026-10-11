#!/usr/bin/env python3
"""Build an sdist-derived wheel and test it outside the source checkout, offline."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*command: str, cwd: Path) -> str:
    return subprocess.run(
        command, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def single(directory: Path, pattern: str) -> Path:
    found = tuple(directory.glob(pattern))
    if len(found) != 1:
        raise ValueError(f"expected one {pattern} in {directory}, found {len(found)}")
    return found[0]


def provide_packaging(site: Path) -> None:
    """Supply only the installed packaging distribution to the offline resolver."""
    dependency = importlib.metadata.distribution("packaging")
    files = dependency.files
    if files is None:
        raise ValueError("packaging has no installed-file inventory")
    for relative in files:
        if relative.parts[0] not in {
            "packaging",
            f"packaging-{dependency.version}.dist-info",
        }:
            raise ValueError(f"unexpected packaging distribution member: {relative}")
        source = Path(str(dependency.locate_file(relative)))
        destination = site / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


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
        run(sys.executable, "-m", "venv", str(environment), cwd=scratch)
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
        provide_packaging(site)
        run(
            str(python),
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
        run(str(entrypoint), "--help", cwd=scratch)
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
        run(str(entrypoint), "sync", "--repository", str(consumer), cwd=scratch)
        run(str(entrypoint), "verify", "--repository", str(consumer), cwd=scratch)
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
