from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from h2h_dev_tools import version_policy
from h2h_dev_tools.dependency_manifest import dependency_manifest

_ROOT = Path(__file__).resolve().parents[1]


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repository: Path, message: str) -> str:
    git(repository, "add", ".")
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "master")
    git(root, "config", "user.name", "Release Test")
    git(root, "config", "user.email", "release@example.invalid")
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "commit", "--allow-empty", "-m", "chore: initialize repository")
    git(root, "switch", "-c", "task")
    monkeypatch.chdir(root)
    return root


def write_project(repository: Path, version: str) -> None:
    text = f'''[project]
name = "release-fixture"
version = "{version}"
dependencies = []
[tool.h2h.version]
release-paths = ["src/**"]
development-paths = ["README.md", "package.json", ".release/**"]
initial-version = "0.1.0"
dedicated-release-commit = true
'''
    (repository / "pyproject.toml").write_text(text)
    (repository / "package.json").write_text('{"devDependencies": {}}')
    receipt: dict[str, Any] = {
        "schema": "h2h.dependency-audit.v1",
        "project_version": version,
        "manifest_sha256": hashlib.sha256(
            dependency_manifest(tomllib.loads(text), {"devDependencies": {}})
        ).hexdigest(),
        "python": [],
        "node": [],
        "review": {"status": "reviewed", "note": "No external fixture dependencies"},
    }
    directory = repository / ".release"
    directory.mkdir(exist_ok=True)
    (directory / "dependency-audit.json").write_text(json.dumps(receipt))


@pytest.mark.parametrize("quote_paths", ["true", "false"])
@pytest.mark.parametrize(
    "name",
    [
        "說明.py",
        "line\nbreak.py",
        "carriage\rreturn.py",
        "pair\r\nbreak.py",
        '"quoted".py',
        "back\\slash.py",
        "space name.py",
        "trailing.py\n",
    ],
)
def test_changed_release_paths_preserve_exact_git_names(
    repository: Path, quote_paths: str, name: str
) -> None:
    git(repository, "config", "core.quotePath", quote_paths)

    def store(kind: str, content: bytes) -> str:
        return (
            subprocess.run(
                ("git", "hash-object", "-t", kind, "-w", "--stdin"),
                cwd=repository,
                input=content,
                capture_output=True,
                check=True,
            )
            .stdout.decode("ascii")
            .strip()
        )

    # Build raw Git trees without requiring the host filesystem or Windows
    # index validation to accept every legal filename authored on POSIX.
    blob = store("blob", b"public runtime behavior\n")
    subtree = store("tree", b"100644 " + name.encode() + b"\0" + bytes.fromhex(blob))
    candidate = store("tree", b"40000 src\0" + bytes.fromhex(subtree))
    assert version_policy._changed_paths("master", candidate) == (f"src/{name}",)


@pytest.mark.parametrize("operation", ["add", "rename-out", "delete"])
def test_unicode_release_paths_require_and_accept_the_correct_version_bump(
    repository: Path, operation: str
) -> None:
    write_project(repository, "0.1.0")
    (repository / "src").mkdir()
    public_path = repository / "src/工具.py"
    if operation != "add":
        public_path.write_text("public runtime behavior\n")
    base = commit(repository, "feat: bootstrap development tools")
    if operation == "add":
        public_path.write_text("public runtime behavior\n")
    elif operation == "rename-out":
        public_path.rename(repository / "README.md")
    else:
        public_path.unlink()
    changed = commit(repository, "fix: change shipped surface")
    expected = {"src/工具.py"}
    if operation == "rename-out":
        expected.add("README.md")
    assert set(version_policy._changed_paths(base, changed)) == expected
    with pytest.raises(ValueError, match="release surface changed without"):
        version_policy.validate(base, changed, changed)
    write_project(repository, "0.1.1")
    task = commit(repository, "chore(release): bump version to 0.1.1")
    version_policy.validate(base, task, task)


def test_initial_release_requires_version_bound_candidate_audit(
    repository: Path,
) -> None:
    checker = version_policy
    base = git(repository, "rev-parse", "master")
    write_project(repository, "0.1.0")
    task = commit(repository, "feat: bootstrap development tools")
    checker.validate(base, task, task)
    (repository / ".release/dependency-audit.json").unlink()
    invalid = commit(repository, "test: remove required evidence")
    with pytest.raises(ValueError, match="dependency audit"):
        checker.validate(base, invalid, invalid)


@pytest.mark.parametrize("version", ["0.0.1", "1.0.0", "0.1.0.1", "00.1.0"])
def test_initial_release_rejects_another_version(
    repository: Path, version: str
) -> None:
    write_project(repository, version)
    task = commit(repository, "feat: bootstrap development tools")
    with pytest.raises(ValueError):
        version_policy.validate("master", task, task)


def test_initial_release_does_not_apply_to_nonempty_missing_metadata(
    repository: Path,
) -> None:
    (repository / "README.md").write_text("Not an empty baseline\n")
    base = commit(repository, "docs: seed content")
    write_project(repository, "0.1.0")
    task = commit(repository, "feat: add package")
    with pytest.raises(ValueError, match="empty baseline"):
        version_policy.validate(base, task, task)


def test_candidate_audit_rejects_stale_dependency_hash(repository: Path) -> None:
    write_project(repository, "0.1.0")
    (repository / "package.json").write_text(
        '{"devDependencies": {"markdownlint-cli2": ">=0.23.2"}}'
    )
    task = commit(repository, "feat: bootstrap development tools")
    with pytest.raises(ValueError, match="does not match dependencies"):
        version_policy.validate("master", task, task)


def test_compatible_tool_release_has_one_dedicated_version_transition(
    repository: Path,
) -> None:
    checker = version_policy
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    commit(repository, "feat: add tool behavior")
    write_project(repository, "0.1.1")
    task = commit(repository, "chore(release): bump version to 0.1.1")
    checker.validate(base, task, task)
    write_project(repository, "0.1.2")
    task = commit(repository, "chore(release): bump version to 0.1.2")
    with pytest.raises(ValueError, match=r"exactly one|dedicated release commit"):
        checker.validate(base, task, task)


def test_development_breaking_signal_does_not_change_tool_lane(
    repository: Path,
) -> None:
    checker = version_policy
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    (repository / "README.md").write_text("Changed contributor instructions\n")
    commit(repository, "docs!: change contributor workflow")
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    commit(repository, "fix: correct tool result")
    write_project(repository, "0.1.1")
    task = commit(repository, "chore(release): bump version to 0.1.1")
    checker.validate(base, task, task)


def test_runtime_change_without_version_and_unknown_paths_fail(
    repository: Path,
) -> None:
    checker = version_policy
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    (repository / "unknown").write_text("unclassified\n")
    task = commit(repository, "test: add unknown file")
    with pytest.raises(ValueError, match="unclassified"):
        checker.validate(base, task, task)
    (repository / "unknown").unlink()
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    task = commit(repository, "feat: add behavior without bump")
    with pytest.raises(ValueError, match="without a project version bump"):
        checker.validate(base, task, task)


def test_candidate_audit_requires_the_full_dependency_inventory(
    repository: Path,
) -> None:
    write_project(repository, "0.1.0")
    manifest = repository / "pyproject.toml"
    manifest.write_text(
        manifest.read_text().replace(
            "dependencies = []", 'dependencies = ["packaging>=26.0"]'
        )
    )
    receipt_path = repository / ".release/dependency-audit.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["manifest_sha256"] = hashlib.sha256(
        dependency_manifest(
            tomllib.loads(manifest.read_text()), {"devDependencies": {}}
        )
    ).hexdigest()
    receipt_path.write_text(json.dumps(receipt))
    task = commit(repository, "feat: bootstrap development tools")
    with pytest.raises(ValueError, match="Python inventory is incomplete"):
        version_policy.validate("master", task, task)


def test_release_commit_cannot_mix_runtime_work_with_version(repository: Path) -> None:
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    write_project(repository, "0.1.1")
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    task = commit(repository, "chore(release): bump version to 0.1.1")
    with pytest.raises(ValueError, match="may only change version and audit"):
        version_policy.validate(base, task, task)


@pytest.mark.parametrize("environment_ref", ["task", "missing-task"])
def test_index_version_uses_the_original_merge_parent_after_task_advances(
    repository: Path, monkeypatch: pytest.MonkeyPatch, environment_ref: str
) -> None:
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    git(repository, "update-ref", "refs/heads/master", base)
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    commit(repository, "fix: add compatible behavior")
    write_project(repository, "0.1.1")
    original_task = commit(repository, "chore(release): bump version to 0.1.1")
    git(repository, "switch", "master")
    git(repository, "merge", "--no-ff", "--no-commit", "task")
    candidate = git(repository, "write-tree")
    later_worktree = repository.parent / "later-worktree"
    git(repository, "worktree", "add", str(later_worktree), "task")
    (later_worktree / "src/tool.py").write_text("value = 'breaking'\n")
    later_task = commit(later_worktree, "feat!: change unrelated later behavior")
    assert later_task != original_task
    monkeypatch.setenv("WORKFLOW_MERGE_TASK_REF", environment_ref)
    selected = version_policy._candidate(
        argparse.Namespace(index=True, base=None, candidate="HEAD")
    )
    assert selected == (base, candidate, f"{base}..{original_task}")
    version_policy._validate(*selected)


@pytest.mark.parametrize("environment_ref", ["task", "HEAD", "missing-task"])
def test_index_without_active_merge_rejects_mutable_task_refs(
    repository: Path, monkeypatch: pytest.MonkeyPatch, environment_ref: str
) -> None:
    monkeypatch.setenv("WORKFLOW_MERGE_TASK_REF", environment_ref)
    with pytest.raises(ValueError, match=r"immutable.*commit"):
        version_policy._candidate(
            argparse.Namespace(index=True, base=None, candidate="HEAD")
        )


def test_direct_pre_merge_hook_validates_an_immutable_parent_before_merge_head(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_project(repository, "0.1.0")
    base = commit(repository, "feat: bootstrap development tools")
    git(repository, "update-ref", "refs/heads/master", base)
    (repository / "src").mkdir()
    (repository / "src/tool.py").write_text("value = 1\n")
    commit(repository, "fix: add compatible behavior")
    write_project(repository, "0.1.1")
    task = commit(repository, "chore(release): bump version to 0.1.1")
    git(repository, "switch", "master")
    hooks = repository.parent / "hooks"
    hooks.mkdir()
    observed = repository.parent / "observed-hook.json"
    hook = hooks / "pre-merge-commit"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import argparse, json\nfrom pathlib import Path\n"
        "from h2h_dev_tools import version_policy\n"
        "merge_head = Path(version_policy._git('rev-parse', '--git-path', 'MERGE_HEAD'))\n"
        "assert not merge_head.exists(), 'fixture must exercise the direct hook timing'\n"
        "selected = version_policy._candidate(argparse.Namespace(index=True))\n"
        "version_policy._validate(*selected)\n"
        f"Path({str(observed)!r}).write_text(json.dumps(selected))\n"
    )
    hook.chmod(0o755)
    git(repository, "config", "core.hooksPath", str(hooks))
    monkeypatch.setenv("PYTHONPATH", str(_ROOT / "src"))
    monkeypatch.setenv("WORKFLOW_MERGE_TASK_REF", task)
    git(repository, "merge", "--no-ff", "--no-edit", "task")
    selected = json.loads(observed.read_text())
    assert selected == [
        base,
        git(repository, "rev-parse", "HEAD^{tree}"),
        f"{base}..{task}",
    ]
    assert git(repository, "show", "-s", "--format=%P", "HEAD").split() == [base, task]
