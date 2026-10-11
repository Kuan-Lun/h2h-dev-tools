"""Local gate and hook entry points preserve Git-authoritative path bytes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX shell entry points")


@pytest.fixture(params=["checkout\ninside", "checkout\n\n"])
def repository(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    name = request.param
    assert isinstance(name, str)
    root = tmp_path / name
    root.mkdir()
    subprocess.run(
        ("git", "init", "-b", "master", str(root)), check=True, capture_output=True
    )
    return root


def _copy(root: Path, relative: str) -> None:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ROOT / relative, destination)


@pytest.mark.parametrize("entry", ["check-fast.sh", "check-full.sh"])
def test_quality_gate_keeps_git_root_with_line_breaks(
    repository: Path, tmp_path: Path, entry: str
) -> None:
    for name in ("check-fast.sh", "check-full.sh"):
        _copy(repository, f"scripts/{name}")
    log = tmp_path / "calls.jsonl"
    for relative in (
        ".venv/bin/ruff",
        ".venv/bin/mypy",
        ".venv/bin/python",
        "node_modules/.bin/markdownlint-cli2",
    ):
        executable = repository / relative
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"with open({str(log)!r}, 'a') as output:\n"
            "    output.write(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv}) + '\\n')\n"
        )
        executable.chmod(0o755)
    nested = repository / "nested"
    nested.mkdir()

    result = subprocess.run(
        ("bash", str(repository / "scripts" / entry)),
        cwd=nested,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == (6 if entry == "check-full.sh" else 4)
    assert all(call["cwd"] == str(repository) for call in calls)


def test_commit_hook_preserves_relative_message_and_git_authority(
    repository: Path, tmp_path: Path
) -> None:
    for relative in (".githooks/commit-msg", "src/h2h_dev_tools/commit-message.sh"):
        _copy(repository, relative)
    message = tmp_path / "message"
    message.write_text("fix: preserve source path\n")
    environment = dict(os.environ)
    environment.update(GIT_DIR=str(repository / ".git"), GIT_WORK_TREE=str(repository))

    result = subprocess.run(
        ("bash", str(repository / ".githooks/commit-msg"), "message"),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_rebase_hook_keeps_primary_protection_in_root_with_line_breaks(
    repository: Path,
) -> None:
    for relative in (
        ".githooks/pre-rebase",
        "scripts/detect-primary-branch.sh",
        "src/h2h_dev_tools/primary-branch.sh",
    ):
        _copy(repository, relative)
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Path Test",
            "-c",
            "user.email=path@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "test: initialize",
        ),
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ("bash", str(repository / ".githooks/pre-rebase"), "upstream", "master"),
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 1
    assert "Rebasing primary is not allowed" in result.stderr


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/check-fast.sh",
        "scripts/check-full.sh",
        ".githooks/commit-msg",
        ".githooks/pre-rebase",
    ],
)
def test_git_authority_query_failure_blocks_local_entry(
    tmp_path: Path, relative: str
) -> None:
    _copy(tmp_path, relative)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    result = subprocess.run(
        ("bash", str(tmp_path / relative), "unused-message"),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 128
    assert "not a git repository" in result.stderr
