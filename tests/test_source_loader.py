"""Verified source registration preserves identity and rejects changed authority."""

from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from h2h_dev_tools import bundle
from h2h_dev_tools.bundle_verify import BUNDLE_DIRECTORY
from h2h_dev_tools.executable_fixture import write_python_executable

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


@pytest.mark.parametrize("cache_mode", ["TIMESTAMP", "UNCHECKED_HASH"])
def test_parent_initializer_uses_source_instead_of_accepted_stale_bytecode(
    source_repository: Path, cache_mode: str
) -> None:
    parent = source_repository / "scripts/_shared/__init__.py"
    stale = b"raise RuntimeError('STALE PARENT CACHE')\n"
    current = b"VALUE = 'current'\n".ljust(len(stale), b" ")
    parent.write_bytes(stale)
    timestamp = parent.stat().st_mtime_ns
    cache = py_compile.compile(
        str(parent),
        cfile=str(
            parent.parent
            / "__pycache__"
            / f"__init__.{sys.implementation.cache_tag}.pyc"
        ),
        doraise=True,
        invalidation_mode=getattr(py_compile.PycInvalidationMode, cache_mode),
    )
    assert cache is not None
    parent.write_bytes(current)
    os.utime(parent, ns=(timestamp, timestamp))
    cached = Path(cache).read_bytes()
    ordinary = _run(source_repository, "import _shared\n")
    assert ordinary.returncode != 0 and "STALE PARENT CACHE" in ordinary.stderr
    result = _run(
        source_repository,
        "bootstrap()\nimport _shared\nassert _shared.VALUE == 'current'\n"
        "first = _shared\nbootstrap()\nassert sys.modules['_shared'] is first\n",
    )
    assert result.returncode == 0, result.stderr
    assert Path(cache).read_bytes() == cached


@pytest.mark.parametrize("namespace_parent", [False, True])
def test_bootstrap_rejects_an_earlier_ordinary_parent_import(
    source_repository: Path, namespace_parent: bool
) -> None:
    if namespace_parent:
        (source_repository / "scripts/_shared/__init__.py").unlink()
    result = _run(source_repository, "import _shared\nbootstrap()\n")
    assert result.returncode != 0
    assert "Package was imported before source bootstrap: _shared" in result.stderr


@pytest.mark.parametrize("namespace_parent", [False, True])
def test_parent_source_identity_is_preserved_and_revalidated(
    source_repository: Path, namespace_parent: bool
) -> None:
    if namespace_parent:
        (source_repository / "scripts/_shared/__init__.py").unlink()
    result = _run(
        source_repository,
        """
first = bootstrap()
parent = sys.modules['_shared']
assert bootstrap() is first
assert sys.modules['_shared'] is parent
(root / 'scripts/_shared/__init__.py').write_text('VALUE = 2\\n')
bootstrap()
""",
    )
    assert result.returncode != 0
    assert "Source package changed after bootstrap" in result.stderr


def test_parent_initializer_can_import_its_source_registered_child(
    source_repository: Path,
) -> None:
    (source_repository / "scripts/_shared/__init__.py").write_text(
        "from . import h2h_dev_tools\nCHILD = h2h_dev_tools\n"
    )
    result = _run(
        source_repository,
        """
package_module = bootstrap()
assert sys.modules['_shared'].CHILD is package_module
assert bootstrap() is package_module
""",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("when", ["initializer", "after-bootstrap"])
def test_parent_search_path_drift_is_rejected(
    source_repository: Path, when: str
) -> None:
    if when == "initializer":
        (source_repository / "scripts/_shared/__init__.py").write_text(
            "__path__ = []\n"
        )
        code = "bootstrap()\n"
    else:
        code = "bootstrap()\nsys.modules['_shared'].__path__ = []\nbootstrap()\n"
    result = _run(source_repository, code)
    assert result.returncode != 0
    assert "Parent package path changed after bootstrap" in result.stderr


def test_bootstrap_pins_the_parent_directory_instead_of_searching_sys_path(
    source_repository: Path,
) -> None:
    decoy = source_repository / "decoy/_shared"
    decoy.mkdir(parents=True)
    (decoy / "__init__.py").write_text("raise RuntimeError('WRONG PARENT SOURCE')\n")
    result = _run(
        source_repository,
        """
sys.path.insert(0, str(root / 'decoy'))
bootstrap()
assert sys.modules['_shared'].__path__ == [str(root / 'scripts/_shared')]
""",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "kind", ["initializer-link", "initializer-directory", "parent-link"]
)
def test_parent_source_types_are_rejected_before_execution(
    source_repository: Path, kind: str
) -> None:
    parent = source_repository / "scripts/_shared"
    initializer = parent / "__init__.py"
    if kind == "parent-link":
        replacement = parent.with_name("moved-parent")
        parent.rename(replacement)
        parent.symlink_to(replacement, target_is_directory=True)
    else:
        initializer.unlink()
        if kind == "initializer-directory":
            initializer.mkdir()
        else:
            replacement = source_repository / "other-init.py"
            replacement.write_text("raise RuntimeError('WRONG PARENT SOURCE')\n")
            initializer.symlink_to(replacement)
    result = _run(source_repository, "bootstrap()\n")
    assert result.returncode != 0
    assert (
        "must be a real directory" in result.stderr
        or "must be a regular file" in result.stderr
    )
    assert "RuntimeError: WRONG PARENT SOURCE" not in result.stderr


def test_explicit_empty_parent_authority_is_not_an_unpinned_request(
    source_repository: Path,
) -> None:
    result = _run(
        source_repository,
        """
runpy.run_path(str(package / 'source_loader.py'))['load_package'](
    package, namespace, expected_parents={})
""",
    )
    assert result.returncode != 0
    assert "Parent packages differ from the controller registration" in result.stderr


@pytest.mark.parametrize("outer_initializer", [False, True])
def test_bootstrap_preserves_each_parent_in_a_mixed_two_level_chain(
    source_repository: Path, outer_initializer: bool
) -> None:
    outer = source_repository / "scripts/__init__.py"
    inner = source_repository / "scripts/_shared/__init__.py"
    if outer_initializer:
        outer.write_text("VALUE = 'outer'\n")
        inner.unlink()
    else:
        inner.write_text("VALUE = 'inner'\n")
    result = _run(
        source_repository,
        """
namespace = 'scripts._shared.h2h_dev_tools'
first = bootstrap()
parents = first.__loader__._h2h_parent_sources
assert set(parents) == {'scripts', 'scripts._shared'}
assert parents['scripts'][0] == str(root / 'scripts')
assert parents['scripts._shared'][0] == str(root / 'scripts/_shared')
assert sum(digest is None for directory, digest in parents.values()) == 1
outer, inner = sys.modules['scripts'], sys.modules['scripts._shared']
assert outer.__path__ == [str(root / 'scripts')]
assert inner.__path__ == [str(root / 'scripts/_shared')]
assert bootstrap() is first
assert sys.modules['scripts'] is outer
assert sys.modules['scripts._shared'] is inner
""",
    )
    assert result.returncode == 0, result.stderr


def _documented_utility_entry(repository: Path, utility: str) -> Path:
    filename = (
        "with-git-environment.py"
        if utility == "git_environment"
        else "check-offline-wheel.py"
    )
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    marker = f"```python\n# scripts/{filename}\n"
    assert readme.count(marker) == 1
    source = readme.split(marker, 1)[1].split("\n```", 1)[0]
    entry = repository / "scripts" / filename
    entry.write_text(source + "\n")
    return entry


@pytest.mark.parametrize("utility", ["git_environment", "offline_wheel"])
@pytest.mark.parametrize(
    "snapshot_state", ["valid", "missing-manifest", "selection", "payload"]
)
def test_documented_utility_verifies_snapshot_before_starting_children(
    tmp_path: Path, utility: str, snapshot_state: str
) -> None:
    component = utility.replace("_", "-")
    configuration = tmp_path / "pyproject.toml"
    configuration.write_text(f'[tool.h2h-dev-tools]\ncomponents = ["{component}"]\n')
    bundle.sync_bundle(tmp_path)
    package = tmp_path / BUNDLE_DIRECTORY
    entry = _documented_utility_entry(tmp_path, utility)
    marker = tmp_path / "command-started"
    if utility == "git_environment":
        arguments = [
            "--repository",
            str(tmp_path),
            "--",
            sys.executable,
            "-I",
            "-c",
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('test child')\n"
            "raise SystemExit(7)\n",
        ]
    else:
        development_python = tmp_path / (
            ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
        )
        write_python_executable(
            development_python,
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('development Python')\n"
            "raise SystemExit(7)\n",
        )
        callback = tmp_path / "smoke.py"
        callback.write_text(
            "raise AssertionError('preparation must stop before callback')\n"
        )
        arguments = ["--repository", str(tmp_path), "--smoke-script", "smoke.py"]
    if snapshot_state == "missing-manifest":
        (package / "manifest.json").unlink()
    elif snapshot_state == "selection":
        configuration.write_text('[tool.h2h-dev-tools]\ncomponents = ["process"]\n')
    elif snapshot_state == "payload":
        source = package / f"{utility}.py"
        source.write_bytes(source.read_bytes() + b"\n# unapproved payload drift\n")
    result = subprocess.run(
        (sys.executable, "-I", "-B", str(entry), *arguments),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if snapshot_state == "valid":
        assert marker.exists(), result.stdout + result.stderr
        assert result.returncode == (7 if utility == "git_environment" else 1)
    else:
        assert not marker.exists(), "Invalid snapshot launched a real child"
        assert result.returncode != 0, result.stdout + result.stderr
        assert "BundleError" in result.stderr, result.stderr


def test_repository_git_environment_entry_ignores_stale_package_bytecode(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    entry = scripts / "with-git-environment.py"
    shutil.copy2(repository / "scripts/with-git-environment.py", entry)
    package = tmp_path / "src/h2h_dev_tools"
    package.mkdir(parents=True)
    for name in ("source_loader.py", "git_environment.py"):
        shutil.copy2(repository / "src/h2h_dev_tools" / name, package / name)
    loaded = tmp_path / "source-loaded"
    initializer = package / "__init__.py"
    current = (
        "from pathlib import Path\n"
        f"Path({str(loaded)!r}).write_text('verified source')\n"
    ).encode()
    stale = b"raise RuntimeError('STALE PACKAGE CACHE')\n".ljust(len(current), b" ")
    initializer.write_bytes(stale)
    timestamp = initializer.stat().st_mtime_ns
    cache = py_compile.compile(
        str(initializer),
        cfile=str(
            package / "__pycache__" / f"__init__.{sys.implementation.cache_tag}.pyc"
        ),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    assert cache is not None
    initializer.write_bytes(current)
    os.utime(initializer, ns=(timestamp, timestamp))
    cached = Path(cache).read_bytes()
    ordinary = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            f"import sys; sys.path.insert(0, {str(package.parent)!r}); import h2h_dev_tools",
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert ordinary.returncode != 0 and "STALE PACKAGE CACHE" in ordinary.stderr
    assert not loaded.exists()
    child = tmp_path / "child-started"
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            str(entry),
            "--repository",
            str(tmp_path),
            "--",
            sys.executable,
            "-I",
            "-c",
            f"from pathlib import Path; Path({str(child)!r}).write_text('child'); raise SystemExit(7)",
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 7, result.stdout + result.stderr
    assert loaded.read_text() == "verified source"
    assert child.read_text() == "child"
    assert Path(cache).read_bytes() == cached


@pytest.mark.parametrize("callback_status", [0, 7])
def test_documented_offline_entry_runs_real_callback_and_cleans_environment(
    tmp_path: Path, callback_status: int
) -> None:
    repository = tmp_path / "consumer checkout"
    repository.mkdir()
    (repository / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    (repository / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[project]\nname = "documented-offline-fixture"\nversion = "1.0.0"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/documented_fixture"]\n'
        '[tool.h2h-dev-tools]\ncomponents = ["offline-wheel"]\n'
    )
    package = repository / "src/documented_fixture"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('VALUE = "candidate wheel"\n')
    bundle.sync_bundle(repository)
    entry = _documented_utility_entry(repository, "offline_wheel")
    evidence = tmp_path / "callback.json"
    callback = repository / "smoke.py"
    callback.write_text(
        "import json, sys\nfrom pathlib import Path\nimport documented_fixture\n"
        "assert sys.flags.isolated\n"
        "assert documented_fixture.VALUE == 'candidate wheel'\n"
        "assert Path(documented_fixture.__file__).is_relative_to(Path(sys.prefix))\n"
        "assert sys.argv[2] == '--literal=$value with spaces'\n"
        "Path(sys.argv[1]).write_text(json.dumps({'prefix': sys.prefix, 'cwd': str(Path.cwd())}))\n"
        f"raise SystemExit({callback_status})\n"
    )
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            str(entry),
            "--repository",
            str(repository),
            "--smoke-script",
            "smoke.py",
            "--",
            str(evidence),
            "--literal=$value with spaces",
        ),
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == (1 if callback_status else 0), (
        result.stdout + result.stderr
    )
    observed = json.loads(evidence.read_text())
    assert not Path(observed["cwd"]).is_relative_to(repository)
    assert not Path(observed["cwd"]).exists()
    assert not Path(observed["prefix"]).exists()
    assert not (repository / "dist").exists()
