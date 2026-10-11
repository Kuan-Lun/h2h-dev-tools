from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_REVIEWER = """import json
import subprocess
import sys
from pathlib import Path

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

directory = Path(git("rev-parse", "--git-common-dir"))
mode = sys.argv[1]
with (directory / "fixture-review-events").open("a") as events:
    events.write(mode + "\\n")
candidate = [git("rev-parse", "HEAD"), git("rev-parse", "MERGE_HEAD"), git("write-tree")]
receipt = directory / "fixture-review"
if mode == "run":
    if (directory / "reject-review").exists():
        raise SystemExit(1)
    receipt.write_text(json.dumps(candidate))
elif mode == "verify":
    assert sys.argv[2:] == ["--index", "--expected-tree", candidate[2]]
    assert json.loads(receipt.read_text()) == candidate
else:
    raise AssertionError(mode)
"""


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def fixture_repository(root: Path, *, succeeds: bool, profile: str = "full") -> str:
    root.mkdir()
    git(root, "init", "-b", "master")
    git(root, "config", "user.name", "Merge Test")
    git(root, "config", "user.email", "merge@example.invalid")
    (root / ".gitignore").write_text(".venv/\n")
    (root / "README.md").write_text("baseline\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "chore: initialize repository")
    base = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-c", "task")
    scripts = root / "scripts"
    scripts.mkdir()
    hooks = root / ".githooks"
    hooks.mkdir()
    for name in (
        "git-flow-merge.sh",
        "detect-primary-branch.sh",
        "release-gate.py",
        "review-merge-candidate.sh",
    ):
        shutil.copy2(_ROOT / "scripts" / name, scripts / name)
    shutil.copytree(
        _ROOT / "src/h2h_dev_tools",
        root / "src/h2h_dev_tools",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in ("pre-commit", "pre-merge-commit", "commit-msg", "pre-rebase"):
        shutil.copy2(_ROOT / ".githooks" / name, hooks / name)
    (scripts / "check-version.py").write_text("print('version fixture passed')\n")
    (scripts / "check_change_scope.py").write_text(f"print({profile!r})\n")
    (scripts / "review-code.py").write_text(_REVIEWER)
    (scripts / "check-docs.py").write_text("print('documentation fixture passed')\n")
    executable(scripts / "check-fast.sh", "#!/bin/sh\nexit 0\n")
    executable(
        scripts / "check-full.sh",
        "#!/bin/sh\n"
        "printf 'checked\\n' >> \"$(git rev-parse --git-path fixture-gate)\"\n"
        f"exit {0 if succeeds else 1}\n",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "feat: add candidate")
    local_bin = root / ".venv/bin"
    local_bin.mkdir(parents=True)
    (local_bin / "python").symlink_to(sys.executable)
    git(root, "config", "core.hooksPath", ".githooks")
    return base


@pytest.mark.parametrize("succeeds", [True, False])
def test_merge_checks_real_candidate_once_and_preserves_task_on_failure(
    tmp_path: Path, succeeds: bool
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=succeeds)
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
    assert (root / ".git/fixture-gate").read_text() == "checked\n"
    expected_reviews = "run\nverify\nverify\n" if succeeds else "run\nverify\n"
    assert (root / ".git/fixture-review-events").read_text() == expected_reviews
    assert not (root / ".git/MERGE_HEAD").exists()
    if succeeds:
        assert git(root, "branch", "--show-current") == "master"
        assert len(git(root, "show", "-s", "--format=%P", "HEAD").split()) == 2
        assert git(root, "branch", "--list", "task") == ""
    else:
        assert git(root, "branch", "--show-current") == "task"
        assert git(root, "rev-parse", "master") == base
        assert git(root, "branch", "--list", "task")
    assert git(root, "status", "--porcelain") == ""


def test_release_gate_rejects_candidate_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    executable(
        root / "scripts/check-full.sh",
        "#!/bin/sh\nprintf 'changed\\n' > README.md\n",
    )
    git(root, "add", ".")
    git(
        root, "-c", "core.hooksPath=/dev/null", "commit", "-m", "test: mutate candidate"
    )
    git(root, "switch", "master")
    git(root, "merge", "--no-ff", "--no-commit", "task")
    subprocess.run(
        (sys.executable, "scripts/review-code.py", "run", "--index"),
        cwd=root,
        check=True,
    )
    result = subprocess.run(
        (sys.executable, "scripts/release-gate.py"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "candidate changed during the gate" in result.stderr


def test_full_gate_rejects_unreviewed_candidate_before_running_checks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True)
    git(root, "switch", "master")
    git(root, "merge", "--no-ff", "--no-commit", "task")
    result = subprocess.run(
        (sys.executable, "scripts/release-gate.py"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not (root / ".git/fixture-gate").exists()
    assert (root / ".git/fixture-review-events").read_text() == "verify\n"


@pytest.mark.parametrize("failure", ["review", "evidence-after-gate"])
def test_review_failure_aborts_merge_and_preserves_task(
    tmp_path: Path, failure: str
) -> None:
    root = tmp_path / "repository"
    base = fixture_repository(root, succeeds=True)
    if failure == "review":
        (root / ".git/reject-review").touch()
    else:
        executable(
            root / "scripts/check-full.sh",
            '#!/bin/sh\nrm "$(git rev-parse --git-path fixture-review)"\n',
        )
        git(root, "add", ".")
        git(root, "commit", "-m", "test: invalidate review during checks")
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert git(root, "rev-parse", "master") == base
    assert git(root, "branch", "--show-current") == "task"
    assert git(root, "branch", "--list", "task")
    assert not (root / ".git/MERGE_HEAD").exists()
    expected = "run\n" if failure == "review" else "run\nverify\nverify\n"
    assert (root / ".git/fixture-review-events").read_text() == expected


def test_documentation_merge_does_not_run_or_verify_review(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    fixture_repository(root, succeeds=True, profile="documentation")
    (root / ".git/reject-review").touch()
    result = subprocess.run(
        ("scripts/git-flow-merge.sh",),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / ".git/fixture-review-events").exists()
    assert not (root / ".git/fixture-gate").exists()
    assert git(root, "branch", "--show-current") == "master"
