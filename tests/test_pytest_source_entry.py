"""Repository pytest entry loads candidate source before plugin imports."""

from __future__ import annotations

import os
import py_compile
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _checkout(tmp_path: Path, module: str) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    package = root / "src/h2h_dev_tools"
    shutil.copytree(
        _ROOT / "src/h2h_dev_tools",
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    scripts = root / "scripts"
    scripts.mkdir()
    for name in ("check-full.sh", "run-pytest.py"):
        shutil.copy2(_ROOT / "scripts" / name, scripts / name)
    (scripts / "check-fast.sh").write_text("#!/bin/sh\nexit 0\n")
    (scripts / "check-fast.sh").chmod(0o755)
    (scripts / "build-and-verify-distributions.py").write_text(
        "from pathlib import Path\nPath('build-reached').touch()\n"
    )
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "PYTEST_", "PYTHON"))
    }
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        PYTHONPATH=os.pathsep.join((str(root / "src"), str(root))),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_PLUGINS="source_authority_probe",
    )
    subprocess.run(
        ("git", "init", "-b", "main"),
        cwd=root,
        env=environment,
        capture_output=True,
        check=True,
        timeout=10,
    )
    path = package / f"{module}.py"
    original = path.read_bytes()
    stale = b"\nraise RuntimeError('STALE PYTEST SOURCE')\n"
    current = b"\n#" + b" " * (len(stale) - 3) + b"\n"
    assert len(stale) == len(current)
    path.write_bytes(original + stale)
    py_compile.compile(
        str(path),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    timestamp = path.stat().st_mtime_ns
    path.write_bytes(original + current)
    os.utime(path, ns=(timestamp, timestamp))
    # Explicit plugins are imported before conftest and test collection. A
    # cached conftest bootstrap therefore cannot satisfy this source contract.
    (root / "source_authority_probe.py").write_text(
        "from pathlib import Path\n"
        f"from h2h_dev_tools import {module} as candidate\n"
        "assert getattr(candidate.__loader__, '_h2h_source_registration', None)\n"
        "Path('plugin-loaded-source').touch()\n"
    )
    (root / "test_candidate.py").write_text(
        "from h2h_dev_tools import version_policy, process\n"
        "def test_candidate_behavior():\n"
        "    assert version_policy._parse_version('1.2.3') == (1, 2, 3)\n"
        "    assert process.ProcessCommand.__module__ == 'h2h_dev_tools.process'\n"
    )
    return root, environment


def _run(
    root: Path, environment: dict[str, str], *command: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("module", ["version_policy", "process"])
@pytest.mark.parametrize(
    "entry",
    [
        "direct",
        pytest.param(
            "full-gate",
            marks=pytest.mark.skipif(
                os.name != "posix", reason="The full gate uses a POSIX shell"
            ),
        ),
    ],
)
def test_pytest_entry_ignores_stale_timestamp_cache_before_plugin_loading(
    tmp_path: Path, module: str, entry: str
) -> None:
    root, environment = _checkout(tmp_path, module)
    ordinary = _run(
        root,
        environment,
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "--collect-only",
        "-p",
        "no:cacheprovider",
    )
    assert ordinary.returncode != 0
    assert "STALE PYTEST SOURCE" in ordinary.stderr
    assert not (root / "plugin-loaded-source").exists()

    command = (
        ("bash", "scripts/check-full.sh")
        if entry == "full-gate"
        else (
            sys.executable,
            "-B",
            "scripts/run-pytest.py",
            "-p",
            "no:cacheprovider",
            "-q",
            "test_candidate.py",
        )
    )
    result = _run(root, environment, *command)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert (root / "plugin-loaded-source").exists()
    assert (root / "build-reached").exists() == (entry == "full-gate")


@pytest.mark.parametrize(
    "arguments,expected",
    [(("-q", "test_failure.py"), 1), (("-q", "-k", "no_such_case"), 5)],
    ids=["failed-test", "empty-selection"],
)
def test_pytest_entry_preserves_selectors_and_failure_exit_codes(
    tmp_path: Path, arguments: tuple[str, ...], expected: int
) -> None:
    root, environment = _checkout(tmp_path, "process")
    (root / "test_failure.py").write_text(
        "def test_expected_failure():\n    assert False, 'deliberate regression oracle'\n"
    )

    result = _run(
        root,
        environment,
        sys.executable,
        "-B",
        "scripts/run-pytest.py",
        "-p",
        "no:cacheprovider",
        *arguments,
    )

    assert result.returncode == expected, result.stdout + result.stderr
    assert (root / "plugin-loaded-source").exists()
