"""Verified source registration preserves identity and rejects changed authority."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from h2h_dev_tools import bundle
from h2h_dev_tools.bundle_verify import BUNDLE_DIRECTORY

_PRELUDE = """
import importlib, runpy, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'scripts'))
package = root / 'scripts/_shared/h2h_dev_tools'
namespace = '_shared.h2h_dev_tools'
def bootstrap(directory=package):
    return runpy.run_path(str(directory / 'source_loader.py'))['load_package'](
        directory, namespace)
"""


@pytest.fixture
def source_repository(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.h2h-dev-tools]\ncomponents = ["process", "version-policy"]\n'
    )
    bundle.sync_bundle(tmp_path)
    (tmp_path / "scripts/_shared/__init__.py").write_text("")
    assert (tmp_path / BUNDLE_DIRECTORY / "source_loader.py").is_file()
    return tmp_path


def _run(repository: Path, code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-I", "-B", "-c", _PRELUDE + code, str(repository)),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_repeated_source_bootstrap_preserves_module_and_class_identity(
    source_repository: Path,
) -> None:
    result = _run(
        source_repository,
        """
first = bootstrap()
process = importlib.import_module(namespace + '.process')
command_type = process.ProcessCommand
second = bootstrap()
assert second is first
assert importlib.import_module(namespace + '.process') is process
assert process.ProcessCommand is command_type
assert len([finder for finder in sys.meta_path
            if getattr(finder, '_h2h_source_registration', (None, None))[1]
            == namespace]) == 1
print('same identity')
""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "same identity"


@pytest.mark.parametrize("change", ["payload", "path"])
def test_bootstrap_rejects_a_new_valid_package_for_an_existing_namespace(
    source_repository: Path, change: str
) -> None:
    replacement = (
        """
import hashlib, json
source = package / 'process.py'
source.write_bytes(source.read_bytes() + b'\\n# New valid source payload\\n')
manifest_path = package / 'manifest.json'
manifest = json.loads(manifest_path.read_text())
manifest['files']['process.py'] = hashlib.sha256(source.read_bytes()).hexdigest()
verifier = runpy.run_path(str(package / 'bundle_verify.py'))
manifest['payload_sha256'] = verifier['payload_digest'](manifest['files'])
manifest_path.write_text(json.dumps(manifest))
verifier['verify_bundle'](root)
replacement = package
"""
        if change == "payload"
        else """
import shutil
other = root.parent / (root.name + '-other')
shutil.copytree(root, other)
replacement = other / 'scripts/_shared/h2h_dev_tools'
"""
    )
    result = _run(
        source_repository,
        "bootstrap()\nfrom _shared.h2h_dev_tools import process\n"
        + replacement
        + "bootstrap(replacement)\n",
    )
    assert result.returncode != 0
    assert "Source package changed after bootstrap" in result.stderr


def test_source_bootstrap_rejects_an_earlier_ordinary_import(
    source_repository: Path,
) -> None:
    result = _run(
        source_repository,
        "from _shared.h2h_dev_tools import process\nbootstrap()\n",
    )
    assert result.returncode != 0
    assert "Package was imported before source bootstrap" in result.stderr


def test_source_loader_supports_relative_imports_and_runpy_cli_execution(
    source_repository: Path,
) -> None:
    result = _run(
        source_repository,
        """
bootstrap()
from _shared.h2h_dev_tools import dependency_manifest
manifest_function = dependency_manifest.dependency_manifest
sys.argv = ['check-version', '--help']
try:
    runpy.run_module(namespace + '.version_policy', run_name='__main__')
except SystemExit as error:
    assert error.code == 0
else:
    raise AssertionError('The real CLI entry did not execute')
version = importlib.import_module(namespace + '.version_policy')
assert version.dependency_manifest is manifest_function
print('relative import and CLI complete')
""",
    )
    assert result.returncode == 0, result.stderr
    assert "--candidate" in result.stdout
    assert "relative import and CLI complete" in result.stdout
