from __future__ import annotations

import subprocess
import time
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from h2h_dev_tools import change_scope


@pytest.fixture
def documented_repository(tmp_path: Path) -> tuple[Path, str, str]:
    def git(*args: str) -> str:
        return subprocess.check_output(("git", *args), cwd=tmp_path).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Deadline fixture")
    git("config", "user.email", "deadline@example.invalid")
    (tmp_path / "README.md").write_text("# Original\n")
    git("add", ".")
    git("commit", "-qm", "test: original document")
    base = git("rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("# Updated\n")
    git("add", ".")
    git("commit", "-qm", "test: updated document")
    return tmp_path, base, git("rev-parse", "HEAD")


def test_generic_classifier_retains_default_and_shares_optional_deadline(
    documented_repository: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base, candidate = documented_repository
    original = subprocess.run
    budgets: list[float] = []

    def observed(*args: Any, **kwargs: Any) -> Any:
        budgets.append(kwargs["timeout"])
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", observed)
    assert change_scope.classify_changes(base, candidate, root) == "documentation"
    assert budgets == [60] * 5
    budgets.clear()
    assert (
        change_scope.classify_changes(
            base, candidate, root, deadline=time.monotonic() + 5
        )
        == "documentation"
    )
    assert len(budgets) == 5 and all(0 < item < 5 for item in budgets)
    assert all(first > second for first, second in pairwise(budgets))


@pytest.mark.parametrize("deadline", [0, float("inf"), float("nan")])
def test_expired_or_invalid_deadline_never_admits_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deadline: float
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Expired classification must not start Git")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(change_scope.ChangeScopeError, match="deadline"):
        change_scope.classify_changes("base", "candidate", tmp_path, deadline=deadline)


def test_late_git_success_cannot_pass_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def late(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.03)
        return subprocess.CompletedProcess(args, 0, b"a" * 40 + b"\n")

    monkeypatch.setattr(subprocess, "run", late)
    with pytest.raises(change_scope.ChangeScopeError, match="deadline"):
        change_scope.resolve_tree(tmp_path, "HEAD", deadline=time.monotonic() + 0.01)
