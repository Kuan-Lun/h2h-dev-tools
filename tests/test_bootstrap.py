from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_RESOURCES = Path(__file__).resolve().parents[1] / "src/h2h_dev_tools"


def invoke(
    resource: str, root: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("bash", str(_RESOURCES / resource), str(root), *arguments),
        capture_output=True,
        text=True,
        check=False,
    )


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    git(root, "init", "-b", "master")
    git(root, "config", "user.name", "Bootstrap Test")
    git(root, "config", "user.email", "bootstrap@example.invalid")
    git(root, "commit", "--allow-empty", "-m", "test: baseline")
    return root


def test_primary_detection_rejects_ambiguity_and_honors_explicit_ref(
    repository: Path,
) -> None:
    git(repository, "branch", "main")
    result = invoke("primary-branch.sh", repository)
    assert result.returncode == 1
    assert "ambiguous" in result.stderr
    git(repository, "config", "workflow.primaryBranch", "master")
    assert invoke("primary-branch.sh", repository).stdout == "master\n"
    git(repository, "config", "workflow.primaryBranch", "absent")
    assert invoke("primary-branch.sh", repository).returncode == 1


@pytest.mark.parametrize("existing", ["path", "executable"])
def test_installer_preserves_existing_hooks(repository: Path, existing: str) -> None:
    if existing == "path":
        git(repository, "config", "core.hooksPath", "my-hooks")
    else:
        hook = repository / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
    result = invoke("install-git-hooks.sh", repository)
    assert result.returncode == 1
    assert "Refusing" in result.stderr
    if existing == "path":
        assert git(repository, "config", "core.hooksPath") == "my-hooks"
    else:
        assert (
            repository / ".git/hooks/pre-commit"
        ).read_text() == "#!/bin/sh\nexit 0\n"


def test_bootstrap_works_without_python_environment_and_passes_explicit_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands.jsonl"
    for name in ("uv", "npm"):
        path = tools / name
        path.write_text(
            "#!/usr/bin/env python3\nimport json, os, sys\n"
            "with open(os.environ['BOOTSTRAP_LOG'], 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv) + '\\n')\n"
        )
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOOTSTRAP_LOG", str(log))
    source = tmp_path / "candidate source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nname="example"\n')
    result = invoke("rebuild-env.sh", root, "", "--source", f"example={source}")
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line)[1:] for line in log.read_text().splitlines()]
    assert commands == [
        ["venv", "--clear", "--python", "3.14", ".venv"],
        [
            "pip",
            "install",
            "--refresh",
            "--python",
            ".venv/bin/python",
            "-e",
            ".[dev]",
            "--editable",
            str(source),
        ],
        [
            "install",
            "--package-lock=false",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ],
    ]
    log.unlink()
    (source / "pyproject.toml").unlink()
    invalid = invoke("rebuild-env.sh", root, "", "--source", f"example={source}")
    assert invalid.returncode == 1
    assert not log.exists(), "invalid source must be rejected before clearing .venv"
    help_result = invoke(
        "rebuild-env.sh", root, "scripts/forbidden-after-help.py", "--help"
    )
    assert help_result.returncode == 0
    assert "Usage:" in help_result.stdout
    assert not log.exists(), "help must not run installation or repository callbacks"
