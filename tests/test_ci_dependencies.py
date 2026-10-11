from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from h2h_dev_tools.ci_dependencies import main

_INSTALLER = Path(__file__).parents[1] / "scripts" / "install-ci-dependencies.py"


def _fixture_installer(tmp_path: Path, manifest: str) -> Path:
    repository = tmp_path / "checkout"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    installer = scripts / _INSTALLER.name
    installer.write_bytes(_INSTALLER.read_bytes())
    (repository / "pyproject.toml").write_text(manifest, encoding="utf-8")
    return installer


def _capture_pip(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is True
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.mark.parametrize("pytest_plugins", [False, True])
def test_ci_install_uses_manifest_constraints_and_required_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, pytest_plugins: bool
) -> None:
    installer = _fixture_installer(
        tmp_path,
        """
[project]
dependencies = ['example-package[extra]>=2; python_version >= "3.14"']
[project.optional-dependencies]
dev = ["pytest>=99,!=99.1", "hypothesis>=88,<89", "unrelated-dev-tool>=42"]
[tool.pytest.ini_options]
required_plugins = ["pytest-xdist>=77,<78", "pytest-custom>=66"]
""",
    )
    calls = _capture_pip(monkeypatch)
    arguments = [str(installer), "pytest", "hypothesis", "Example_Package"]
    if pytest_plugins:
        arguments.append("--pytest-plugins")
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.chdir(tmp_path)

    main(repository=installer.parents[1])

    expected = [
        "pytest>=99,!=99.1",
        "hypothesis>=88,<89",
        'example-package[extra]>=2; python_version >= "3.14"',
    ]
    if pytest_plugins:
        expected.extend(["pytest-xdist>=77,<78", "pytest-custom>=66"])
    assert calls == [[sys.executable, "-m", "pip", "install", "--upgrade", *expected]]


def test_ci_install_preserves_intersecting_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _fixture_installer(
        tmp_path,
        """
[project]
dependencies = ["pytest>=9"]
[project.optional-dependencies]
dev = ["pytest<10"]
[tool.pytest.ini_options]
required_plugins = ["pytest>=9"]
""",
    )
    calls = _capture_pip(monkeypatch)
    monkeypatch.setattr(sys, "argv", [str(installer), "--pytest-plugins", "pytest"])

    main(repository=installer.parents[1])

    assert calls == [
        [sys.executable, "-m", "pip", "install", "--upgrade", "pytest>=9", "pytest<10"]
    ]


@pytest.mark.parametrize(
    "arguments", [["pytest"], ["--pytest-plugins", "pytest-xdist"]]
)
def test_ci_install_rejects_missing_declarations_before_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    installer = _fixture_installer(
        tmp_path,
        '[project.optional-dependencies]\ndev = ["pytest-xdist>=77"]\n',
    )
    calls = _capture_pip(monkeypatch)
    monkeypatch.setattr(sys, "argv", [str(installer), *arguments])

    with pytest.raises(SystemExit) as error:
        main(repository=installer.parents[1])

    assert error.value.code == 2
    assert not calls


def test_ci_install_propagates_pip_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _fixture_installer(
        tmp_path, '[project.optional-dependencies]\ndev = ["pytest>=99"]\n'
    )

    def fail(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is True
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(sys, "argv", [str(installer), "pytest"])

    with pytest.raises(subprocess.CalledProcessError):
        main(repository=installer.parents[1])


def test_ci_source_entry_reads_its_checkout_outside_working_directory(
    tmp_path: Path,
) -> None:
    installer = _fixture_installer(
        tmp_path, '[project.optional-dependencies]\ndev = ["pytest>=99"]\n'
    )
    source = _INSTALLER.parents[1] / "src/h2h_dev_tools"
    shutil.copytree(
        source,
        installer.parents[1] / "src/h2h_dev_tools",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            """
import json, runpy, subprocess, sys
subprocess.run = lambda command, **kwargs: print(json.dumps(command))
sys.argv = [sys.argv[1], 'pytest']
runpy.run_path(sys.argv[0], run_name='__main__')
""",
            str(installer),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pytest>=99",
    ]
