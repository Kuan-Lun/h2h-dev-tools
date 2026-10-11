from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "src/h2h_dev_tools/commit-message.sh"
_PATTERN = r"^(docs|fix|test)(\([a-z0-9._/-]+\))?(!)?: .+"


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
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
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Commit Message Test")
    _git(root, "config", "user.email", "commit-message@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "commit", "--allow-empty", "-m", "test: initial history")
    return root


def _invoke(
    repository: Path,
    message_file: Path,
    pattern: str = _PATTERN,
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("bash", str(_HELPER), str(repository), str(message_file), pattern),
        cwd=repository if cwd is None else cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _install_hook(repository: Path) -> None:
    hook = repository / ".git/hooks/commit-msg"
    hook.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"exec bash {shlex.quote(str(_HELPER))} {shlex.quote(str(repository))} "
        f'"$1" {shlex.quote(_PATTERN)}\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)


def test_nonmerge_commit_cannot_use_merge_subject_exemption(repository: Path) -> None:
    _install_hook(repository)
    original = _git(repository, "rev-parse", "HEAD")

    result = subprocess.run(
        ("git", "commit", "--allow-empty", "-m", "Merge a normal task change"),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "Conventional Commits" in result.stderr
    assert _git(repository, "rev-parse", "HEAD") == original


def _prepare_merge(repository: Path) -> tuple[str, str]:
    primary = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "-c", "task/example")
    (repository / "payload.txt").write_text("task content\n", encoding="utf-8")
    _git(repository, "add", "payload.txt")
    _git(repository, "commit", "-m", "fix: add the task payload")
    task = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "main")
    _git(repository, "merge", "--no-ff", "--no-commit", "task/example")
    assert _git(repository, "rev-parse", "MERGE_HEAD") == task
    return primary, task


def test_real_two_parent_merge_can_use_merge_subject(repository: Path) -> None:
    _install_hook(repository)
    primary, task = _prepare_merge(repository)

    _git(repository, "commit", "-m", "Merge branch 'task/example' into main")

    assert _git(repository, "show", "-s", "--format=%P", "HEAD").split() == [
        primary,
        task,
    ]


@pytest.mark.parametrize(
    ("message", "expected"), [("ticket-42: complete work\n", 0), ("fix: work\n", 1)]
)
def test_repository_supplies_the_allowed_pattern(
    repository: Path, message: str, expected: int
) -> None:
    message_file = repository / "message.txt"
    message_file.write_text(message, encoding="utf-8")

    result = _invoke(repository, message_file, r"^ticket-[0-9]+: .+$")

    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("pattern", ["", "["], ids=["empty", "invalid"])
@pytest.mark.parametrize("pending_merge", [False, True], ids=["normal", "merge"])
def test_unusable_pattern_is_rejected(
    repository: Path, pattern: str, pending_merge: bool
) -> None:
    if pending_merge:
        _prepare_merge(repository)
    message_file = repository / "message.txt"
    message_file.write_text(
        "Merge an otherwise valid candidate\n" if pending_merge else "fix: valid\n",
        encoding="utf-8",
    )

    assert _invoke(repository, message_file, pattern).returncode != 0


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_unreadable_message_is_rejected(repository: Path, kind: str) -> None:
    message_file = repository / "message.txt"
    if kind == "directory":
        message_file.mkdir()

    assert _invoke(repository, message_file).returncode != 0


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("fix: final line without newline", 0),
        ("fix(scope)!: first line\n\nArbitrary commit body.\n", 0),
        ("invalid first line\nfix: later lines do not repair it\n", 1),
        ("", 1),
    ],
)
def test_only_the_first_line_defines_the_subject(
    repository: Path, message: str, expected: int
) -> None:
    message_file = repository / "message.txt"
    message_file.write_text(message, encoding="utf-8")

    result = _invoke(repository, message_file)

    assert result.returncode == expected, result.stderr


def test_relative_message_path_uses_callers_directory(
    repository: Path, tmp_path: Path
) -> None:
    _prepare_merge(repository)
    caller = tmp_path / "caller with spaces"
    caller.mkdir()
    (caller / "message.txt").write_text("Merge caller message\n", encoding="utf-8")
    (repository / "message.txt").write_text(
        "invalid repository message\n", encoding="utf-8"
    )

    result = _invoke(repository, Path("message.txt"), cwd=caller)

    assert result.returncode == 0, result.stderr
