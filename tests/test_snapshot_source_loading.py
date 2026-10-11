"""Verified snapshot source, not a matching timestamp cache, is executed."""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from h2h_dev_tools import bundle
from h2h_dev_tools.bundle_verify import BUNDLE_DIRECTORY, payload_digest, verify_bundle


def _snapshot(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.h2h-dev-tools]\ncomponents = ["process"]\n'
    )
    bundle.sync_bundle(tmp_path)
    (tmp_path / "scripts/_shared/__init__.py").write_text("")
    return tmp_path / BUNDLE_DIRECTORY


def _update_manifest(directory: Path) -> None:
    path = directory / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"] = {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in manifest["files"]
    }
    manifest["payload_sha256"] = payload_digest(manifest["files"])
    path.write_text(json.dumps(manifest))


def _run(root: Path, code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


_BOOTSTRAP = """
import runpy, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'scripts'))
package = root / 'scripts/_shared/h2h_dev_tools'
runpy.run_path(str(package / 'source_loader.py'))['load_package'](
    package, '_shared.h2h_dev_tools')
"""


@pytest.mark.parametrize(
    "name", ["__init__.py", "process.py", "bundle_verify.py", "source_loader.py"]
)
def test_snapshot_bootstrap_ignores_stale_equal_size_timestamp_cache(
    tmp_path: Path,
    name: str,
) -> None:
    directory = _snapshot(tmp_path)
    path = directory / name
    source = path.read_bytes()
    stale = b"\nraise RuntimeError('STALE BYTECODE')\n"
    current = b"\n#" + b" " * (len(stale) - 3) + b"\n"
    assert len(stale) == len(current)
    path.write_bytes(source + stale)
    py_compile.compile(str(path), doraise=True)
    timestamp = path.stat().st_mtime_ns
    path.write_bytes(source + current)
    os.utime(path, ns=(timestamp, timestamp))
    _update_manifest(directory)
    verify_bundle(tmp_path)
    # -B prevents cache writes, but this ordinary import still executes old code.
    module = name.removesuffix(".py")
    ordinary = (
        "import sys; sys.path.insert(0, sys.argv[1] + '/scripts'); "
        f"import _shared.h2h_dev_tools{'' if module == '__init__' else '.' + module}"
    )
    stale_result = _run(tmp_path, ordinary)
    assert stale_result.returncode != 0
    assert "STALE BYTECODE" in stale_result.stderr
    result = _run(
        tmp_path,
        _BOOTSTRAP
        + "\nfrom _shared.h2h_dev_tools import process\nprint(process.ProcessCommand.__name__)\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ProcessCommand"


@pytest.mark.parametrize("corruption", ["selection", "source", "missing-manifest"])
def test_snapshot_bootstrap_rejects_unverified_source(
    tmp_path: Path,
    corruption: str,
) -> None:
    directory = _snapshot(tmp_path)
    if corruption == "selection":
        (tmp_path / "pyproject.toml").write_text(
            "[tool.h2h-dev-tools]\ncomponents = []\n"
        )
    elif corruption == "source":
        (directory / "process.py").write_text(
            "raise RuntimeError('must not execute')\n"
        )
    else:
        (directory / "manifest.json").unlink()
    result = _run(tmp_path, _BOOTSTRAP)
    assert result.returncode != 0
    assert "must not execute" not in result.stderr


def test_snapshot_import_rechecks_bytes_after_bootstrap(tmp_path: Path) -> None:
    _snapshot(tmp_path)
    result = _run(
        tmp_path,
        _BOOTSTRAP
        + """
(package / 'process.py').write_text("raise RuntimeError('must not execute')\\n")
from _shared.h2h_dev_tools import process
""",
    )
    assert result.returncode != 0
    assert "Source changed after bootstrap" in result.stderr
    assert "must not execute" not in result.stderr
