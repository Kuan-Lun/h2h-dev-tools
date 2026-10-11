from __future__ import annotations

import json
import os
import subprocess
import sys
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


@pytest.mark.parametrize("directory_search", ["unset", "current", "foreign"])
def test_installer_resolves_relative_root_once_before_primary_query(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory_search: str,
) -> None:
    foreign_parent = tmp_path / "foreign search"
    foreign = foreign_parent / repository.name
    foreign.mkdir(parents=True)
    git(foreign, "init", "-b", "main")
    git(foreign, "config", "user.name", "Foreign Bootstrap Test")
    git(foreign, "config", "user.email", "foreign@example.invalid")
    git(foreign, "commit", "--allow-empty", "-m", "test: independent checkout")
    foreign_config = (foreign / ".git/config").read_bytes()
    monkeypatch.delenv("CDPATH", raising=False)
    if directory_search != "unset":
        monkeypatch.setenv(
            "CDPATH", "." if directory_search == "current" else str(foreign_parent)
        )

    result = subprocess.run(
        ("bash", str(_RESOURCES / "install-git-hooks.sh"), repository.name),
        cwd=repository.parent,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "Installed repository Git hooks; primary branch: master\n"
    assert git(repository, "config", "--local", "core.hooksPath") == ".githooks"
    assert (
        git(repository, "config", "--local", "branch.master.mergeOptions") == "--no-ff"
    )
    assert git(repository, "config", "--local", "branch.master.rebase") == "false"
    assert git(repository, "config", "--local", "pull.rebase") == "false"
    assert git(repository, "config", "--local", "pull.ff") == "only"
    assert (foreign / ".git/config").read_bytes() == foreign_config


@pytest.mark.parametrize("invalid_primary", ["ambiguous", "missing"])
def test_installer_rejects_invalid_primary_without_partial_configuration(
    repository: Path, invalid_primary: str
) -> None:
    if invalid_primary == "ambiguous":
        git(repository, "branch", "main")
    else:
        git(repository, "config", "workflow.primaryBranch", "absent")
    original_config = (repository / ".git/config").read_bytes()

    result = invoke("install-git-hooks.sh", repository)

    assert result.returncode != 0
    assert (repository / ".git/config").read_bytes() == original_config


@pytest.mark.parametrize("python_layout", ["bin/python", "Scripts/python.exe"])
@pytest.mark.parametrize("with_callback", [False, True])
def test_bootstrap_works_without_python_environment_and_passes_explicit_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    python_layout: str,
    with_callback: bool,
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
            "from pathlib import Path\n"
            "with open(os.environ['BOOTSTRAP_LOG'], 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv) + '\\n')\n"
            "if sys.argv[1:2] == ['venv']:\n"
            f"    python = Path(sys.argv[-1]) / {python_layout!r}\n"
            "    python.parent.mkdir(parents=True)\n"
            f"    python.symlink_to({sys.executable!r})\n"
            "if sys.argv[1:3] == ['pip', 'install']:\n"
            "    python = Path(sys.argv[sys.argv.index('--python') + 1])\n"
            "    assert python.is_file(), f'missing install interpreter: {python}'\n"
            "if sys.argv[1:3] == ['pip', 'compile']:\n"
            "    requirements = Path(sys.argv[5])\n"
            "    Path(os.environ['BOOTSTRAP_LOG'] + '.requirements').write_text(\n"
            "        requirements.read_text())\n"
        )
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOOTSTRAP_LOG", str(log))
    source = tmp_path / "candidate source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nname="example"\n')
    callback = root / "after build.py"
    callback.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "Path('callback.json').write_text(json.dumps([str(Path.cwd()), sys.executable]))\n"
    )
    result = invoke(
        "rebuild-env.sh",
        root,
        callback.name if with_callback else "",
        "--source",
        f"example={source}",
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line)[1:] for line in log.read_text().splitlines()]
    assert commands[0][:3] == ["venv", "--python", "3.14"]
    assert commands[1][:3] == ["pip", "compile", "--python"]
    assert commands[2:] == [
        ["venv", "--clear", "--python", "3.14", ".venv"],
        [
            "pip",
            "install",
            "--refresh",
            "--python",
            f".venv/{python_layout}",
            "--requirements",
            commands[1][4],
        ],
        [
            "install",
            "--package-lock=false",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ],
    ]
    assert Path(str(log) + ".requirements").read_text() == (
        f"-e example @ {source.as_uri()}\n-e .[dev]\n"
    )
    if with_callback:
        assert json.loads((root / "callback.json").read_text()) == [
            str(root),
            str(root / ".venv" / python_layout),
        ]
    else:
        assert not (root / "callback.json").exists()
    log.unlink()
    (source / "pyproject.toml").unlink()
    invalid = invoke("rebuild-env.sh", root, "", "--source", f"example={source}")
    assert invalid.returncode != 0
    assert all(
        "--clear" not in json.loads(line) for line in log.read_text().splitlines()
    ), "invalid source must be rejected before clearing .venv"
    log.unlink()
    help_result = invoke(
        "rebuild-env.sh", root, "scripts/forbidden-after-help.py", "--help"
    )
    assert help_result.returncode == 0
    assert "Usage:" in help_result.stdout
    assert not log.exists(), "help must not run installation or repository callbacks"
