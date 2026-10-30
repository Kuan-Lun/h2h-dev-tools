from __future__ import annotations

import os
import py_compile
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from h2h_dev_tools import process


@pytest.fixture(autouse=True)
def isolated_review_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disposable reviews own their estimates, independent of the parent merge."""
    for name in (
        "H2H_CODE_REVIEW_TIMEOUT_SECONDS",
        "H2H_CODE_REVIEW_MIN_SHARD_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated_git_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disposable repositories own their config; keep Git-local authority intact."""
    for name in tuple(os.environ):
        if name in {
            "GIT_CONFIG",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
        } or re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", name):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def poisoned_source_package(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """Poison only a private exact source copy with valid timestamp caches."""
    source = Path(process.__file__).resolve().parent
    package = tmp_path / "source" / "h2h_dev_tools"
    package.mkdir(parents=True)
    for path in source.iterdir():
        if path.suffix in {".py", ".sh"}:
            shutil.copy2(path, package / path.name)
    name = str(request.param)
    path = package / name
    original = path.read_bytes()
    info = path.stat()
    poison = b"raise AssertionError('stale source bytecode executed')\n"
    path.write_bytes(poison + b" " * (len(original) - len(poison)))
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    cache = package / "__pycache__" / f"{path.stem}.{sys.implementation.cache_tag}.pyc"
    cache.parent.mkdir(exist_ok=True)
    py_compile.compile(
        str(path),
        cfile=str(cache),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    path.write_bytes(original)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    # Confirm that this cache is actually eligible under an isolated interpreter.
    control = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            f"from h2h_dev_tools import {path.stem}",
            str(package.parent),
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert (
        control.returncode != 0 and "stale source bytecode executed" in control.stderr
    )
    return package
